from datetime import date

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from tests.conftest import FakeEdgar
from trackrecord.config import Settings
from trackrecord.edgar.parsing import FilingRecord
from trackrecord.ingest.runner import run_company_sync, run_document_fetch
from trackrecord.ingest.storage import RawStore
from trackrecord.ingest.sync import (
    classify_filing,
    filings_missing_documents,
    load_universe,
    seed_companies,
)
from trackrecord.models import (
    Company,
    Document,
    Filing,
    FilingKind,
    FiscalPeriod,
    SyncRun,
    SyncStatus,
    XbrlFact,
)


def record(form: str, items: tuple[str, ...] = ()) -> FilingRecord:
    return FilingRecord("a", form, date(2026, 1, 1), None, None, items, None)


@pytest.mark.parametrize(
    ("form", "items", "expected"),
    [
        ("8-K", ("2.02", "9.01"), (FilingKind.EARNINGS_RELEASE, False)),
        ("8-K/A", ("2.02",), (FilingKind.EARNINGS_RELEASE, True)),
        ("8-K", ("5.02",), None),
        ("10-Q", (), (FilingKind.QUARTERLY_REPORT, False)),
        ("10-K/A", (), (FilingKind.ANNUAL_REPORT, True)),
        ("4", (), None),
        ("10-KT", (), None),
    ],
)
def test_classify_filing(
    form: str, items: tuple[str, ...], expected: tuple[FilingKind, bool] | None
) -> None:
    assert classify_filing(record(form, items)) == expected


def test_universe_has_40_unique_companies() -> None:
    universe = load_universe()
    assert any(c["ticker"] == "ON" for c in universe)
    assert len(universe) == 40
    assert len({c["ticker"] for c in universe}) == 40
    assert len({c["cik"] for c in universe}) == 40


async def seed_nvda(sessionmaker: async_sessionmaker[AsyncSession]) -> int:
    async with sessionmaker() as session:
        await seed_companies(session, [{"ticker": "NVDA", "cik": 1045810}])
        await session.commit()
        return (await session.scalars(select(Company.id))).one()


async def count(session: AsyncSession, model: type) -> int:
    return await session.scalar(select(func.count()).select_from(model)) or 0


@pytest.mark.db
async def test_sync_company_end_to_end(
    sessionmaker: async_sessionmaker[AsyncSession], fake_edgar: FakeEdgar, settings: Settings
) -> None:
    company_id = await seed_nvda(sessionmaker)

    async with fake_edgar.client() as client:
        stats = await run_company_sync(sessionmaker, client, settings, company_id)

    assert stats["calendar_warnings"] == []
    async with sessionmaker() as session:
        company = await session.get_one(Company, company_id)
        assert company.name == "NVIDIA CORP"
        assert company.fiscal_year_end == "0131"
        assert company.last_synced_at is not None

        filings = (await session.scalars(select(Filing))).all()
        kinds = {f.accession: f.kind for f in filings}
        # Only earnings 8-Ks (Item 2.02) are kept; other 8-Ks and Form 4s are not.
        assert kinds["0001045810-26-000073"] == FilingKind.EARNINGS_RELEASE
        assert "0001045810-26-000078" not in kinds  # 8-K Item 8.01
        assert kinds["0001045810-26-000075"] == FilingKind.QUARTERLY_REPORT
        assert kinds["0001045810-26-000021"] == FilingKind.ANNUAL_REPORT
        assert len(filings) == 14  # 7 releases + 5 10-Qs + 2 10-Ks since 2025

        assert stats["facts_inserted"] == stats["facts_seen"] > 0
        assert await count(session, XbrlFact) == stats["facts_inserted"]

        run = (await session.scalars(select(SyncRun))).one()
        assert run.status == SyncStatus.SUCCEEDED and run.finished_at is not None


@pytest.mark.db
async def test_sync_builds_calendar_and_links_filings(
    sessionmaker: async_sessionmaker[AsyncSession], fake_edgar: FakeEdgar, settings: Settings
) -> None:
    company_id = await seed_nvda(sessionmaker)
    async with fake_edgar.client() as client:
        await run_company_sync(sessionmaker, client, settings, company_id)

    async with sessionmaker() as session:
        periods = {p.label: p for p in (await session.scalars(select(FiscalPeriod))).all()}
        q2 = periods["FY2027 Q2"]
        assert (q2.start_date, q2.end_date, q2.is_estimated) == (
            date(2026, 4, 27),
            date(2026, 7, 26),
            False,
        )
        assert periods["FY2027 Q3"].is_estimated
        assert periods["FY2026"].end_date == date(2026, 1, 25)

        filings = {
            f.accession: f
            for f in (
                await session.scalars(select(Filing).options(selectinload(Filing.period)))
            ).all()
        }
        # The 10-Q and the earnings release for the same quarter share a period.
        quarterly = filings["0001045810-26-000075"]
        release = filings["0001045810-26-000073"]
        assert quarterly.period is not None and quarterly.period.label == "FY2027 Q2"
        assert release.period is not None and release.period.label == "FY2027 Q2"
        annual = filings["0001045810-26-000021"]
        assert annual.period is not None and annual.period.label == "FY2026"


@pytest.mark.db
async def test_resync_is_idempotent(
    sessionmaker: async_sessionmaker[AsyncSession], fake_edgar: FakeEdgar, settings: Settings
) -> None:
    company_id = await seed_nvda(sessionmaker)
    async with fake_edgar.client() as client:
        await run_company_sync(sessionmaker, client, settings, company_id)
        async with sessionmaker() as session:
            before = [await count(session, m) for m in (Filing, XbrlFact, FiscalPeriod)]

        stats = await run_company_sync(sessionmaker, client, settings, company_id)

    async with sessionmaker() as session:
        after = [await count(session, m) for m in (Filing, XbrlFact, FiscalPeriod)]
        assert await count(session, SyncRun) == 2
    assert stats["facts_inserted"] == 0
    assert after == before


@pytest.mark.db
async def test_fetch_documents_stores_selected_files(
    sessionmaker: async_sessionmaker[AsyncSession], fake_edgar: FakeEdgar, settings: Settings
) -> None:
    company_id = await seed_nvda(sessionmaker)
    store = RawStore(settings.raw_dir)
    async with fake_edgar.client() as client:
        await run_company_sync(sessionmaker, client, settings, company_id)
        async with sessionmaker() as session:
            pending = await filings_missing_documents(session, company_id)
        assert len(pending) == 14

        for filing_id in pending:
            await run_document_fetch(sessionmaker, client, store, filing_id)
        requests_after_first_pass = len(fake_edgar.requests)
        for filing_id in pending:
            assert await run_document_fetch(sessionmaker, client, store, filing_id) == 0
        assert len(fake_edgar.requests) == requests_after_first_pass

    async with sessionmaker() as session:
        assert await filings_missing_documents(session, company_id) == []
        release = await session.scalar(
            select(Filing)
            .where(Filing.accession == "0001045810-26-000073")
            .options(selectinload(Filing.documents))
        )
        assert release is not None
        assert sorted(d.doc_type for d in release.documents) == ["EX-99.1", "EX-99.2"]
        report = await session.scalar(
            select(Filing)
            .where(Filing.accession == "0001045810-26-000075")
            .options(selectinload(Filing.documents))
        )
        assert report is not None
        assert [d.doc_type for d in report.documents] == ["10-Q"]

        document = (await session.scalars(select(Document).limit(1))).one()
        assert document.storage_path is not None
        content = store.read(document.storage_path)
        assert content.startswith(b"<html>/Archives/edgar/data/1045810/")
        assert document.size_bytes == len(content)


@pytest.mark.db
async def test_failed_sync_is_recorded(
    sessionmaker: async_sessionmaker[AsyncSession], settings: Settings
) -> None:
    company_id = await seed_nvda(sessionmaker)
    failing = FakeEdgar()
    failing.routes.clear()

    async with failing.client() as client:
        with pytest.raises(Exception, match="404"):
            await run_company_sync(sessionmaker, client, settings, company_id)

    async with sessionmaker() as session:
        run = (await session.scalars(select(SyncRun))).one()
        assert run.status == SyncStatus.FAILED
        assert run.error is not None and "404" in run.error


@pytest.mark.db
async def test_resync_removes_periods_no_longer_in_the_calendar(
    sessionmaker: async_sessionmaker[AsyncSession], fake_edgar: FakeEdgar, settings: Settings
) -> None:
    company_id = await seed_nvda(sessionmaker)
    async with sessionmaker() as session:
        session.add(
            FiscalPeriod(
                company_id=company_id,
                fiscal_year=2019,
                fiscal_quarter=2,
                start_date=date(2018, 5, 1),
                end_date=date(2018, 7, 29),
            )
        )
        await session.commit()

    async with fake_edgar.client() as client:
        await run_company_sync(sessionmaker, client, settings, company_id)

    async with sessionmaker() as session:
        years = (await session.scalars(select(FiscalPeriod.fiscal_year))).all()
        assert 2019 not in years
