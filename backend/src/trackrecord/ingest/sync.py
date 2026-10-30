"""Sync one company from EDGAR: filings, XBRL facts, fiscal calendar, documents.

Every step is an upsert keyed on EDGAR identifiers, so re-running a sync only
adds what is new.
"""

import logging
import posixpath
from collections.abc import Iterable, Sequence
from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta
from importlib import resources
from typing import Any

import yaml
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from trackrecord.config import Settings
from trackrecord.edgar.client import EdgarClient, archive_url
from trackrecord.edgar.parsing import (
    FactRecord,
    FilingRecord,
    IndexEntry,
    iter_facts,
    parse_filings_table,
)
from trackrecord.ingest.periods import PeriodicReport, derive_calendar
from trackrecord.ingest.storage import RawStore
from trackrecord.models import Company, Document, Filing, FilingKind, FiscalPeriod, XbrlFact

log = logging.getLogger(__name__)

EARNINGS_ITEM = "2.02"  # 8-K Item 2.02: Results of Operations and Financial Condition
FACT_BATCH_SIZE = 2000
# An earnings release reports the quarter that ended shortly before it was filed.
EARNINGS_RELEASE_MAX_LAG = timedelta(days=75)


def load_universe() -> list[dict[str, Any]]:
    text = resources.files("trackrecord").joinpath("universe.yaml").read_text()
    companies = yaml.safe_load(text)["companies"]
    for company in companies:
        # YAML reads unquoted tickers like ON as booleans.
        if not isinstance(company["ticker"], str) or not isinstance(company["cik"], int):
            raise ValueError(f"bad universe entry {company!r}: quote the ticker")
    return companies


async def seed_companies(session: AsyncSession, universe: Iterable[dict[str, Any]]) -> int:
    rows = [{"cik": c["cik"], "ticker": c["ticker"]} for c in universe]
    stmt = insert(Company).values(rows)
    stmt = stmt.on_conflict_do_update(index_elements=["cik"], set_={"ticker": stmt.excluded.ticker})
    await session.execute(stmt)
    return len(rows)


def classify_filing(record: FilingRecord) -> tuple[FilingKind, bool] | None:
    """Return (kind, is_amendment) for filings we track, None for everything else."""
    base, _, suffix = record.form.partition("/")
    is_amendment = suffix == "A"
    if base == "8-K" and EARNINGS_ITEM in record.items:
        return FilingKind.EARNINGS_RELEASE, is_amendment
    if base == "10-Q":
        return FilingKind.QUARTERLY_REPORT, is_amendment
    if base == "10-K":
        return FilingKind.ANNUAL_REPORT, is_amendment
    return None


async def sync_company(
    session: AsyncSession, client: EdgarClient, company: Company, settings: Settings
) -> dict[str, Any]:
    """Sync filings, facts and the fiscal calendar. Documents are fetched separately."""
    submissions = await client.submissions(company.cik)
    company.name = submissions.get("name")
    company.sic_description = submissions.get("sicDescription")
    company.fiscal_year_end = submissions.get("fiscalYearEnd")

    records = parse_filings_table(submissions["filings"]["recent"])
    for page in submissions["filings"].get("files", []):
        if date.fromisoformat(page["filingTo"]) >= settings.history_start:
            records += parse_filings_table(await client.submissions_page(page["name"]))
    filings_upserted = await upsert_filings(session, company, records, settings.history_start)

    facts_json = await client.company_facts(company.cik)
    facts = (
        list(iter_facts(facts_json, settings.xbrl_taxonomies, settings.history_start))
        if facts_json
        else []
    )
    facts_inserted = await insert_facts(session, company, facts)

    warnings = await rebuild_fiscal_calendar(session, company)
    company.last_synced_at = datetime.now(UTC)
    return {
        "filings_seen": filings_upserted,
        "facts_seen": len(facts),
        "facts_inserted": facts_inserted,
        "calendar_warnings": warnings,
    }


async def upsert_filings(
    session: AsyncSession, company: Company, records: Sequence[FilingRecord], since: date
) -> int:
    rows = []
    for record in records:
        classified = classify_filing(record)
        if classified is None or record.filed_at < since:
            continue
        kind, is_amendment = classified
        rows.append(
            {
                "company_id": company.id,
                "accession": record.accession,
                "form": record.form,
                "kind": kind,
                "is_amendment": is_amendment,
                "filed_at": record.filed_at,
                "accepted_at": record.accepted_at,
                "report_date": record.report_date,
                "items": list(record.items),
                "primary_document": record.primary_document,
            }
        )
    if not rows:
        return 0
    stmt = insert(Filing).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=["accession"],
        set_={
            col: stmt.excluded[col]
            for col in ("form", "kind", "is_amendment", "report_date", "items", "primary_document")
        },
    )
    await session.execute(stmt)
    return len(rows)


async def insert_facts(session: AsyncSession, company: Company, facts: Sequence[FactRecord]) -> int:
    inserted = 0
    for i in range(0, len(facts), FACT_BATCH_SIZE):
        batch = [{"company_id": company.id, **asdict(f)} for f in facts[i : i + FACT_BATCH_SIZE]]
        stmt = insert(XbrlFact).values(batch).on_conflict_do_nothing().returning(XbrlFact.id)
        inserted += len((await session.execute(stmt)).all())
    return inserted


async def rebuild_fiscal_calendar(session: AsyncSession, company: Company) -> list[str]:
    filings = (await session.scalars(select(Filing).where(Filing.company_id == company.id))).all()
    reports = [
        PeriodicReport(f.accession, f.report_date, f.kind == FilingKind.ANNUAL_REPORT)
        for f in filings
        if f.kind != FilingKind.EARNINGS_RELEASE and not f.is_amendment and f.report_date
    ]

    cal = derive_calendar(reports)
    if not cal.periods:
        return cal.warnings

    stmt = insert(FiscalPeriod).values(
        [
            {
                "company_id": company.id,
                "fiscal_year": p.fiscal_year,
                "fiscal_quarter": p.fiscal_quarter,
                "start_date": p.start,
                "end_date": p.end,
                "is_estimated": p.is_estimated,
            }
            for p in cal.periods
        ]
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["company_id", "fiscal_year", "fiscal_quarter"],
        set_={col: stmt.excluded[col] for col in ("start_date", "end_date", "is_estimated")},
    )
    await session.execute(stmt)

    # Drop periods the new calendar no longer contains (e.g. a projection that a
    # filing has since replaced with a differently numbered quarter).
    keep = {(p.fiscal_year, p.fiscal_quarter) for p in cal.periods}
    existing = (
        await session.scalars(select(FiscalPeriod).where(FiscalPeriod.company_id == company.id))
    ).all()
    for period in existing:
        if (period.fiscal_year, period.fiscal_quarter) not in keep:
            await session.delete(period)
    await session.flush()

    await link_filings_to_periods(session, company, filings)
    return cal.warnings


async def link_filings_to_periods(
    session: AsyncSession, company: Company, filings: Sequence[Filing]
) -> None:
    periods = (
        await session.scalars(select(FiscalPeriod).where(FiscalPeriod.company_id == company.id))
    ).all()
    by_end = {(p.end_date, p.fiscal_quarter is None): p for p in periods}
    quarters = sorted((p for p in periods if p.fiscal_quarter), key=lambda p: p.end_date)

    for filing in filings:
        period: FiscalPeriod | None = None
        if filing.kind == FilingKind.EARNINGS_RELEASE:
            # The latest quarter that ended before the release was filed.
            candidates = [
                q
                for q in quarters
                if q.end_date < filing.filed_at <= q.end_date + EARNINGS_RELEASE_MAX_LAG
            ]
            period = candidates[-1] if candidates else None
        elif filing.report_date is not None:
            is_annual = filing.kind == FilingKind.ANNUAL_REPORT
            period = by_end.get((filing.report_date, is_annual))
        filing.period_id = period.id if period else None


def select_documents(filing: Filing, entries: Sequence[IndexEntry]) -> list[IndexEntry]:
    """The files we keep: press-release exhibits (EX-99.x), or a report's main document."""
    if filing.kind == FilingKind.EARNINGS_RELEASE:
        return [e for e in entries if e.doc_type.startswith("EX-99")]
    return [e for e in entries if e.doc_type == filing.form]


async def fetch_filing_documents(
    session: AsyncSession, client: EdgarClient, store: RawStore, filing: Filing, cik: int
) -> int:
    entries = await client.filing_index(cik, filing.accession)
    selected = select_documents(filing, entries)
    if not selected:
        log.warning(
            "%s: no documents selected from %d index entries", filing.accession, len(entries)
        )

    for entry in selected:
        url = archive_url(cik, filing.accession, entry.filename)
        content = await client.get_bytes(url)
        suffix = posixpath.splitext(entry.filename)[1].lower() or ".bin"
        sha, relative = store.put(content, suffix)
        values = {
            "doc_type": entry.doc_type,
            "description": entry.description,
            "url": url,
            "size_bytes": len(content),
            "sha256": sha,
            "storage_path": relative,
            "fetched_at": datetime.now(UTC),
        }
        stmt = insert(Document).values(filing_id=filing.id, filename=entry.filename, **values)
        stmt = stmt.on_conflict_do_update(index_elements=["filing_id", "filename"], set_=values)
        await session.execute(stmt)

    await session.execute(
        update(Filing).where(Filing.id == filing.id).values(documents_fetched_at=datetime.now(UTC))
    )
    return len(selected)


async def filings_missing_documents(
    session: AsyncSession, company_id: int | None = None
) -> list[int]:
    stmt = select(Filing.id).where(Filing.documents_fetched_at.is_(None)).order_by(Filing.filed_at)
    if company_id is not None:
        stmt = stmt.where(Filing.company_id == company_id)
    return list((await session.scalars(stmt)).all())
