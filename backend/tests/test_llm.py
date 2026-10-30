from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.fakes import FakeLlmClient, response
from trackrecord.llm import BudgetExceeded, Llm, LlmError, cost_usd, month_spend
from trackrecord.models import LlmBatch, LlmBatchStatus, LlmCall, LlmCallStatus
from trackrecord.promises.schema import ExtractionOutput


def test_cost_of_a_haiku_request() -> None:
    # 10K input at $0.10/M + 2K output at $0.50/M
    assert cost_usd("claude-haiku-5-5", input_tokens=10_000, output_tokens=2_000) == Decimal(
        "0.002"
    )


def test_haiku_long_prompts_cost_more() -> None:
    short = cost_usd("claude-haiku-5-5", input_tokens=100_000, output_tokens=0)
    long = cost_usd("claude-haiku-5-5", input_tokens=100_001, output_tokens=0)
    assert short == Decimal("0.01")
    assert long > 4 * short


def test_batch_is_half_price_and_cache_reads_are_cheap() -> None:
    full = cost_usd("claude-sonnet-5-5", input_tokens=1_000_000, output_tokens=0)
    assert cost_usd("claude-sonnet-5-5", input_tokens=1_000_000, output_tokens=0, batch=True) == (
        full / 2
    )
    cached = cost_usd("claude-sonnet-5-5", input_tokens=0, output_tokens=0, cache_read_tokens=10**6)
    assert cached == Decimal("0.20")


def make_llm(
    sessionmaker: async_sessionmaker[AsyncSession], client: FakeLlmClient, budget: str = "30"
) -> Llm:
    return Llm(client, sessionmaker, Decimal(budget))


async def call(llm: Llm) -> object:
    return await llm.parse(
        task="test",
        subject="document:1",
        model="claude-haiku-5-5",
        system="system",
        user="user",
        output_format=ExtractionOutput,
        effort="low",
    )


@pytest.mark.db
async def test_successful_call_is_recorded(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    client = FakeLlmClient(lambda kwargs: response(ExtractionOutput(promises=[])))
    result = await call(make_llm(sessionmaker, client))

    request = client.messages.calls[0]
    assert request["model"] == "claude-haiku-5-5"
    assert request["output_format"] is ExtractionOutput
    assert request["output_config"] == {"effort": "low"}
    assert request["system"][0]["cache_control"] == {"type": "ephemeral"}

    async with sessionmaker() as session:
        record = (await session.scalars(select(LlmCall))).one()
    assert record.id == result.call_id  # type: ignore[attr-defined]
    assert record.status == LlmCallStatus.SUCCEEDED
    assert (record.input_tokens, record.output_tokens) == (1000, 200)
    assert record.cost_usd == cost_usd("claude-haiku-5-5", input_tokens=1000, output_tokens=200)
    assert record.request_id == "req_test"


@pytest.mark.db
@pytest.mark.parametrize(
    ("stop_reason", "status"),
    [("refusal", LlmCallStatus.REFUSED), ("max_tokens", LlmCallStatus.TRUNCATED)],
)
async def test_unusable_responses_are_recorded_and_raise(
    sessionmaker: async_sessionmaker[AsyncSession], stop_reason: str, status: LlmCallStatus
) -> None:
    client = FakeLlmClient(lambda kwargs: response(None, stop_reason=stop_reason))

    with pytest.raises(LlmError) as error:
        await call(make_llm(sessionmaker, client))

    assert error.value.status == status
    async with sessionmaker() as session:
        record = (await session.scalars(select(LlmCall))).one()
    assert record.status == status
    assert record.cost_usd > 0  # a refused or truncated request is still billed


@pytest.mark.db
async def test_budget_is_checked_before_calling(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with sessionmaker() as session:
        session.add(
            LlmCall(
                task="old",
                model="claude-haiku-5-5",
                status=LlmCallStatus.SUCCEEDED,
                cost_usd=Decimal("0.9999"),
            )
        )
        await session.commit()
    client = FakeLlmClient(lambda kwargs: response(ExtractionOutput(promises=[])))

    with pytest.raises(BudgetExceeded):
        await call(make_llm(sessionmaker, client, budget="1"))
    assert client.messages.calls == []


@pytest.mark.db
async def test_month_spend_includes_uncollected_batches(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with sessionmaker() as session:
        session.add_all(
            [
                LlmCall(task="t", model="m", status=LlmCallStatus.SUCCEEDED, cost_usd=Decimal("1")),
                LlmCall(
                    task="t",
                    model="m",
                    status=LlmCallStatus.SUCCEEDED,
                    cost_usd=Decimal("5"),
                    created_at=datetime(2020, 1, 1, tzinfo=UTC),
                ),
                LlmBatch(
                    anthropic_batch_id="b1",
                    task="t",
                    model="m",
                    request_count=1,
                    status=LlmBatchStatus.SUBMITTED,
                    estimated_cost_usd=Decimal("2"),
                ),
                LlmBatch(
                    anthropic_batch_id="b2",
                    task="t",
                    model="m",
                    request_count=1,
                    status=LlmBatchStatus.COLLECTED,
                    estimated_cost_usd=Decimal("7"),
                ),
            ]
        )
        await session.commit()
        assert await month_spend(session) == Decimal(3)
