from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.fakes import NVDA_Q2FY27_OUTPUT, FakeClaude, batch_error, batch_success
from trackrecord.config import Settings
from trackrecord.ingest.storage import RawStore
from trackrecord.llm import BudgetExceeded, cost_usd
from trackrecord.models import Document, LlmBatch, LlmBatchStatus, LlmCall, LlmCallStatus, Promise
from trackrecord.promises import batch

pytestmark = pytest.mark.db


def no_direct_calls(kwargs: dict[str, object]) -> object:
    raise AssertionError("batch code must not call messages.parse")


async def submit(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    client: FakeClaude,
    filing_id: int,
    budget: str = "30",
) -> batch.Submitted:
    return await batch.submit(
        sessionmaker,
        client,
        RawStore(settings.raw_dir),
        [filing_id],
        model="claude-haiku-5-5",
        effort="medium",
        monthly_budget_usd=Decimal(budget),
    )


async def test_submit_and_collect(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    release_id: int,
) -> None:
    client = FakeClaude(no_direct_calls)
    submitted = await submit(sessionmaker, settings, client, release_id)

    assert submitted.requests == 2
    (requests,) = client.messages.batches.created
    params = requests[0]["params"]
    assert params["model"] == "claude-haiku-5-5"
    assert params["output_config"]["effort"] == "medium"
    assert params["output_config"]["format"]["type"] == "json_schema"
    assert "promises" in params["output_config"]["format"]["schema"]["properties"]

    # Still running: nothing collected.
    assert (await batch.collect(sessionmaker, client)).still_running == 1

    async with sessionmaker() as session:
        documents = (await session.scalars(select(Document.id).order_by(Document.filename))).all()
    remote = client.messages.batches
    remote.status = "ended"
    remote.results_by_id["msgbatch_1"] = [
        batch_success(f"document-{documents[0]}", NVDA_Q2FY27_OUTPUT),
        batch_error(f"document-{documents[1]}"),
    ]
    collected = await batch.collect(sessionmaker, client)

    assert (collected.batches, collected.failed_requests) == (1, 1)
    assert collected.stats is not None and collected.stats.stored == 6
    async with sessionmaker() as session:
        record = (await session.scalars(select(LlmBatch))).one()
        assert record.status == LlmBatchStatus.COLLECTED
        calls = (await session.scalars(select(LlmCall).order_by(LlmCall.id))).all()
        assert [c.status for c in calls] == [LlmCallStatus.SUCCEEDED, LlmCallStatus.FAILED]
        assert calls[0].cost_usd == cost_usd(
            "claude-haiku-5-5", input_tokens=1000, output_tokens=200, batch=True
        )
        assert len((await session.scalars(select(Promise))).all()) == 6

    # The failed document is submitted again; the succeeded one is not.
    resubmitted = await submit(sessionmaker, settings, client, release_id)
    assert resubmitted.requests == 1
    assert client.messages.batches.created[1][0]["custom_id"] == f"document-{documents[1]}"


async def test_submit_respects_the_budget(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    release_id: int,
) -> None:
    client = FakeClaude(no_direct_calls)

    with pytest.raises(BudgetExceeded):
        await submit(sessionmaker, settings, client, release_id, budget="0.0001")
    assert client.messages.batches.created == []
