from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.fakes import NVDA_Q2FY27_OUTPUT, FakeClaude, promise, response
from trackrecord.config import Settings
from trackrecord.ingest.storage import RawStore
from trackrecord.llm import Llm
from trackrecord.models import (
    Basis,
    Document,
    Metric,
    Promise,
    PromiseStatus,
    ReviewStatus,
)
from trackrecord.promises.extract import (
    extract_filing,
    mark_reviewed,
    refresh_supersession,
    releases_to_extract,
)
from trackrecord.promises.schema import ExtractionOutput

pytestmark = pytest.mark.db


async def extract(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    client: FakeClaude,
    filing_id: int,
    **kwargs: bool,
):
    llm = Llm(client, sessionmaker, Decimal(30))
    return await extract_filing(
        sessionmaker,
        llm,
        RawStore(settings.raw_dir),
        filing_id,
        model="claude-haiku-5-5",
        effort="medium",
        **kwargs,
    )


async def promises(session: AsyncSession) -> list[Promise]:
    return list((await session.scalars(select(Promise).order_by(Promise.id))).all())


async def test_extracts_and_stores_normalized_promises(
    sessionmaker: async_sessionmaker[AsyncSession], settings: Settings, release_id: int
) -> None:
    client = FakeClaude(lambda kwargs: response(NVDA_Q2FY27_OUTPUT))

    stats = await extract(sessionmaker, settings, client, release_id)

    # Two exhibits (EX-99.1 and EX-99.2) with the same guidance: stored once.
    assert len(client.messages.calls) == 2
    assert (stats.extracted, stats.stored, stats.duplicates) == (12, 6, 6)
    assert stats.rejected == [] and stats.needs_review == 0

    message = client.messages.calls[0]["messages"][0]["content"]
    assert "This release reports results for fiscal FY2027 Q2." in message
    assert "The next fiscal period is FY2027 Q3." in message
    assert "Revenue is expected to be $108.0 billion, plus or minus 2%." in message

    async with sessionmaker() as session:
        stored = await promises(session)
        revenue = stored[0]
        assert revenue.metric == Metric.REVENUE
        assert (revenue.low, revenue.high) == (Decimal("105840000000.0"), Decimal("110160000000.0"))
        assert revenue.issued_at == date(2026, 8, 26)
        assert revenue.review_status == ReviewStatus.AUTO_ACCEPTED
        assert revenue.status == PromiseStatus.PENDING
        assert revenue.target_period_id is not None
        document = await session.get_one(Document, revenue.document_id)
        assert document.text is not None
        assert document.text[revenue.span_start : revenue.span_end] == revenue.quote

        tax = next(p for p in stored if p.metric == Metric.TAX_RATE)
        assert (tax.target_fiscal_year, tax.target_fiscal_quarter) == (2027, None)
        assert tax.target_period_id is not None  # FY2027 is a projected period


async def test_rejects_past_periods_and_invented_quotes(
    sessionmaker: async_sessionmaker[AsyncSession], settings: Settings, release_id: int
) -> None:
    output = ExtractionOutput(
        promises=[
            promise(fiscal_quarter=2),  # the quarter being reported
            promise(quote="Revenue is expected to be $500 billion."),
            promise(fiscal_year=2029, fiscal_quarter=1),  # beyond the projected calendar
        ]
    )
    client = FakeClaude(lambda kwargs: response(output))

    stats = await extract(sessionmaker, settings, client, release_id)

    assert sorted(r.split(": ", 1)[1] for r in stats.rejected[:2]) == [
        "not a future period: FY2027 Q2",
        "quote not found in document",
    ]
    async with sessionmaker() as session:
        (stored,) = await promises(session)
    assert stored.target_period_id is None
    assert stored.review_status == ReviewStatus.NEEDS_REVIEW
    assert stored.review_reasons == ["FY2029 Q1 not in calendar"]
    assert stats.not_next_quarter == 1


async def test_skips_documents_already_extracted(
    sessionmaker: async_sessionmaker[AsyncSession], settings: Settings, release_id: int
) -> None:
    client = FakeClaude(lambda kwargs: response(NVDA_Q2FY27_OUTPUT))
    await extract(sessionmaker, settings, client, release_id)
    async with sessionmaker() as session:
        assert release_id not in await releases_to_extract(session)

    await extract(sessionmaker, settings, client, release_id)
    assert len(client.messages.calls) == 2  # no new calls

    await extract(sessionmaker, settings, client, release_id, force=True)
    assert len(client.messages.calls) == 4
    async with sessionmaker() as session:
        assert len(await promises(session)) == 6  # replaced, not duplicated


async def test_later_guidance_supersedes_earlier(
    sessionmaker: async_sessionmaker[AsyncSession], settings: Settings, release_id: int
) -> None:
    client = FakeClaude(lambda kwargs: response(NVDA_Q2FY27_OUTPUT))
    await extract(sessionmaker, settings, client, release_id)

    async with sessionmaker() as session:
        tax = (
            await session.scalars(
                select(Promise).where(
                    Promise.metric == Metric.TAX_RATE, Promise.basis == Basis.GAAP
                )
            )
        ).one()
        # The same full-year guidance, given earlier in the year.
        earlier = Promise(
            **{
                c.key: getattr(tax, c.key)
                for c in Promise.__table__.columns
                if c.key not in {"id", "created_at", "status", "superseded_by_id"}
            }
        )
        earlier.issued_at = date(2026, 5, 20)
        session.add(earlier)
        await session.flush()
        await refresh_supersession(session, tax.company_id)
        await session.commit()

        assert earlier.status == PromiseStatus.SUPERSEDED
        assert earlier.superseded_by_id == tax.id
        assert tax.status == PromiseStatus.PENDING

        # Rejecting the newer one in review reinstates the earlier guidance.
        await mark_reviewed(session, tax.id, ReviewStatus.REJECTED)
        await session.commit()
        await session.refresh(earlier)
        assert earlier.status == PromiseStatus.PENDING
        assert earlier.superseded_by_id is None
