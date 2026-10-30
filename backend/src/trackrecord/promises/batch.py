"""Backfill extraction through the Message Batches API (50% cheaper, results within 24h).

`submit` sends one request per pending press-release exhibit; `collect` stores
the results of every finished batch. Both are safe to re-run.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import anthropic
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trackrecord.db import session_scope
from trackrecord.ingest.storage import RawStore
from trackrecord.llm import apply_usage, check_budget, cost_usd, estimate_tokens
from trackrecord.models import (
    Document,
    Filing,
    LlmBatch,
    LlmBatchStatus,
    LlmCall,
    LlmCallStatus,
)
from trackrecord.promises.extract import (
    TASK,
    ExtractionStats,
    already_extracted,
    apply_extraction,
    build_user_message,
    load_context,
    release_documents,
    subject,
)
from trackrecord.promises.prompt import SYSTEM_PROMPT
from trackrecord.promises.schema import ExtractionOutput

log = logging.getLogger(__name__)

MAX_TOKENS = 16_000
OUTPUT_ESTIMATE = 4_000
_CUSTOM_ID_PREFIX = "document-"


def request_params(model: str, user: str, effort: str) -> dict[str, Any]:
    return {
        "model": model,
        "max_tokens": MAX_TOKENS,
        "system": [{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": user}],
        "output_config": {
            "effort": effort,
            "format": {
                "type": "json_schema",
                "schema": anthropic.transform_schema(ExtractionOutput),
            },
        },
    }


@dataclass
class Submitted:
    batch_id: int | None
    requests: int
    estimated_cost_usd: Decimal


async def submit(
    sessionmaker: async_sessionmaker[AsyncSession],
    client: Any,
    store: RawStore,
    filing_ids: list[int],
    *,
    model: str,
    effort: str,
    monthly_budget_usd: Decimal,
) -> Submitted:
    requests: list[dict[str, Any]] = []
    estimate = Decimal(0)
    async with session_scope(sessionmaker) as session:
        for filing_id in filing_ids:
            filing = await session.get_one(Filing, filing_id)
            ctx = await load_context(session, filing)
            for document in await release_documents(session, store, filing):
                if await already_extracted(session, document):
                    continue
                user = build_user_message(ctx, document)
                requests.append(
                    {
                        "custom_id": f"{_CUSTOM_ID_PREFIX}{document.id}",
                        "params": request_params(model, user, effort),
                    }
                )
                estimate += cost_usd(
                    model,
                    input_tokens=estimate_tokens(SYSTEM_PROMPT + user),
                    output_tokens=OUTPUT_ESTIMATE,
                    batch=True,
                )
        if not requests:
            return Submitted(None, 0, Decimal(0))
        await check_budget(session, monthly_budget_usd, estimate)

    batch = await client.messages.batches.create(requests=requests)
    async with session_scope(sessionmaker) as session:
        record = LlmBatch(
            anthropic_batch_id=batch.id,
            task=TASK,
            model=model,
            request_count=len(requests),
            status=LlmBatchStatus.SUBMITTED,
            estimated_cost_usd=estimate,
        )
        session.add(record)
        await session.flush()
        return Submitted(record.id, len(requests), estimate)


@dataclass
class Collected:
    batches: int = 0
    still_running: int = 0
    failed_requests: int = 0
    stats: ExtractionStats | None = None


async def collect(sessionmaker: async_sessionmaker[AsyncSession], client: Any) -> Collected:
    result = Collected(stats=ExtractionStats())
    assert result.stats is not None
    async with sessionmaker() as session:
        open_batches = (
            await session.scalars(
                select(LlmBatch).where(LlmBatch.status == LlmBatchStatus.SUBMITTED)
            )
        ).all()

    for batch in open_batches:
        remote = await client.messages.batches.retrieve(batch.anthropic_batch_id)
        if remote.processing_status != "ended":
            result.still_running += 1
            continue
        async for item in await client.messages.batches.results(batch.anthropic_batch_id):
            stats = await _store_result(sessionmaker, batch, item)
            if stats is None:
                result.failed_requests += 1
            else:
                result.stats.add(stats)
        async with session_scope(sessionmaker) as session:
            record = await session.get_one(LlmBatch, batch.id)
            record.status = LlmBatchStatus.COLLECTED
            record.collected_at = datetime.now(UTC)
        result.batches += 1
    return result


async def _store_result(
    sessionmaker: async_sessionmaker[AsyncSession], batch: LlmBatch, item: Any
) -> ExtractionStats | None:
    document_id = int(item.custom_id.removeprefix(_CUSTOM_ID_PREFIX))
    async with session_scope(sessionmaker) as session:
        document = await session.get_one(Document, document_id)
        call = LlmCall(
            task=batch.task,
            model=batch.model,
            batch_id=batch.id,
            subject=subject(document),
            status=LlmCallStatus.FAILED,
        )
        session.add(call)

        if item.result.type != "succeeded":
            call.error = f"batch result {item.result.type}"
            log.warning("%s: %s", item.custom_id, call.error)
            return None

        message = item.result.message
        call.request_id = getattr(message, "id", None)
        apply_usage(call, batch.model, message.usage, batch=True)
        if message.stop_reason in {"refusal", "max_tokens"}:
            call.status = (
                LlmCallStatus.REFUSED
                if message.stop_reason == "refusal"
                else LlmCallStatus.TRUNCATED
            )
            call.error = f"stop reason {message.stop_reason}"
            return None
        text = next((block.text for block in message.content if block.type == "text"), "")
        try:
            output = ExtractionOutput.model_validate_json(text)
        except ValidationError as exc:
            call.status = LlmCallStatus.INVALID_OUTPUT
            call.error = str(exc)[:2000]
            return None

        call.status = LlmCallStatus.SUCCEEDED
        await session.flush()
        filing = await session.get_one(Filing, document.filing_id)
        ctx = await load_context(session, filing)
        return await apply_extraction(session, ctx, document, output, call.id)
