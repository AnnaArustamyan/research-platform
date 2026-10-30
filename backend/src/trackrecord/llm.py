"""Claude calls with a cost ledger and a hard monthly budget (docs/design.md §7).

Every request is recorded in `llm_calls` with its token usage and cost. Before a
request, the month's spend plus the request's estimated cost is checked against
the budget, so background jobs stop instead of overspending.
"""

import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import anthropic
from anthropic.types import Usage
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trackrecord.config import Settings
from trackrecord.db import session_scope
from trackrecord.models import LlmBatch, LlmBatchStatus, LlmCall, LlmCallStatus

MILLION = Decimal(1_000_000)


@dataclass(frozen=True)
class Price:
    """USD per million tokens."""

    input: Decimal
    output: Decimal
    cache_read: Decimal
    # Claude Haiku 5.5 charges more for prompts over 100K tokens.
    long_prompt_threshold: int | None = None
    long_input: Decimal | None = None
    long_output: Decimal | None = None


# First-party Claude API list prices as of 2026-10. Cache writes cost 1.25x input.
PRICES: dict[str, Price] = {
    "claude-haiku-5-5": Price(
        Decimal("0.10"),
        Decimal("0.50"),
        Decimal("0.01"),
        long_prompt_threshold=100_000,
        long_input=Decimal("0.50"),
        long_output=Decimal("2.50"),
    ),
    "claude-sonnet-5-5": Price(Decimal("2"), Decimal("10"), Decimal("0.20")),
    "claude-opus-5-5": Price(Decimal("4"), Decimal("20"), Decimal("0.20")),
}
BATCH_DISCOUNT = Decimal("0.5")
CACHE_WRITE_MULTIPLIER = Decimal("1.25")


def cost_usd(
    model: str,
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    batch: bool = False,
) -> Decimal:
    price = PRICES[model]
    prompt_tokens = input_tokens + cache_read_tokens + cache_write_tokens
    input_rate, output_rate = price.input, price.output
    if price.long_prompt_threshold and prompt_tokens > price.long_prompt_threshold:
        assert price.long_input is not None and price.long_output is not None
        input_rate, output_rate = price.long_input, price.long_output
    total = (
        input_tokens * input_rate
        + cache_write_tokens * input_rate * CACHE_WRITE_MULTIPLIER
        + cache_read_tokens * price.cache_read
        + output_tokens * output_rate
    ) / MILLION
    return total * BATCH_DISCOUNT if batch else total


def estimate_tokens(text: str) -> int:
    """A rough upper estimate for budget checks (English filings: ~3.5 chars per token)."""
    return int(len(text) / 3.0) + 1


class BudgetExceeded(Exception):
    pass


class LlmError(Exception):
    def __init__(self, status: LlmCallStatus, message: str) -> None:
        super().__init__(message)
        self.status = status


async def month_spend(session: AsyncSession, now: datetime | None = None) -> Decimal:
    """This month's recorded cost, plus the estimate for batches not yet collected."""
    now = now or datetime.now(UTC)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    recorded = await session.scalar(
        select(func.coalesce(func.sum(LlmCall.cost_usd), 0)).where(
            LlmCall.created_at >= month_start
        )
    )
    in_flight = await session.scalar(
        select(func.coalesce(func.sum(LlmBatch.estimated_cost_usd), 0)).where(
            LlmBatch.status == LlmBatchStatus.SUBMITTED
        )
    )
    return Decimal(recorded or 0) + Decimal(in_flight or 0)


async def check_budget(
    session: AsyncSession, monthly_budget_usd: Decimal, estimated_cost: Decimal
) -> None:
    spent = await month_spend(session)
    if spent + estimated_cost > monthly_budget_usd:
        raise BudgetExceeded(
            f"monthly LLM budget ${monthly_budget_usd} would be exceeded "
            f"(spent ${spent:.4f}, this request ~${estimated_cost:.4f})"
        )


@dataclass
class ParsedCall[T: BaseModel]:
    output: T
    call_id: int


class Llm:
    def __init__(
        self,
        client: Any,  # anthropic.AsyncAnthropic, or a test double with `messages.parse`
        sessionmaker: async_sessionmaker[AsyncSession],
        monthly_budget_usd: Decimal,
    ) -> None:
        self._client = client
        self._sessionmaker = sessionmaker
        self.monthly_budget_usd = monthly_budget_usd

    async def parse[T: BaseModel](
        self,
        *,
        task: str,
        subject: str,
        model: str,
        system: str,
        user: str,
        output_format: type[T],
        effort: str,
        max_tokens: int = 16_000,
        max_output_estimate: int = 4_000,
    ) -> ParsedCall[T]:
        """One structured-output request, recorded in the ledger whatever its outcome."""
        estimate = cost_usd(
            model,
            input_tokens=estimate_tokens(system + user),
            output_tokens=max_output_estimate,
        )
        async with self._sessionmaker() as session:
            await check_budget(session, self.monthly_budget_usd, estimate)

        started = time.monotonic()
        record = LlmCall(task=task, model=model, subject=subject, status=LlmCallStatus.FAILED)
        try:
            response = await self._client.messages.parse(
                model=model,
                max_tokens=max_tokens,
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": user}],
                output_format=output_format,
                output_config={"effort": effort},
            )
        except anthropic.APIError as exc:
            record.error = f"{type(exc).__name__}: {exc}"
            await self._save(record, started)
            raise

        record.request_id = getattr(response, "_request_id", None)
        apply_usage(record, model, response.usage, batch=False)
        output, record.status, record.error = interpret_response(response)
        call_id = await self._save(record, started)
        if output is None:
            raise LlmError(record.status, record.error or "no output")
        return ParsedCall(output=output, call_id=call_id)

    async def _save(self, record: LlmCall, started: float) -> int:
        record.latency_ms = int((time.monotonic() - started) * 1000)
        async with session_scope(self._sessionmaker) as session:
            session.add(record)
            await session.flush()
            return record.id


def apply_usage(record: LlmCall, model: str, usage: Usage, *, batch: bool) -> None:
    record.input_tokens = usage.input_tokens
    record.output_tokens = usage.output_tokens
    record.cache_read_tokens = usage.cache_read_input_tokens or 0
    record.cache_write_tokens = usage.cache_creation_input_tokens or 0
    record.cost_usd = cost_usd(
        model,
        input_tokens=record.input_tokens,
        output_tokens=record.output_tokens,
        cache_read_tokens=record.cache_read_tokens,
        cache_write_tokens=record.cache_write_tokens,
        batch=batch,
    )


def interpret_response(response: Any) -> tuple[Any, LlmCallStatus, str | None]:
    """(parsed output or None, status, error) for a parsed Messages API response."""
    if response.stop_reason == "refusal":
        details = getattr(response, "stop_details", None)
        category = getattr(details, "category", None)
        return None, LlmCallStatus.REFUSED, f"refused (category: {category})"
    if response.stop_reason == "max_tokens":
        return None, LlmCallStatus.TRUNCATED, "output hit max_tokens"
    output = getattr(response, "parsed_output", None)
    if output is None:
        return None, LlmCallStatus.INVALID_OUTPUT, "no parsed output"
    return output, LlmCallStatus.SUCCEEDED, None


def has_claude_credentials() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def make_llm(sessionmaker: async_sessionmaker[AsyncSession], settings: Settings) -> Llm:
    return Llm(anthropic.AsyncAnthropic(), sessionmaker, settings.llm_monthly_budget_usd)
