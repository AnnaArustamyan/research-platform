"""Background jobs, run with:

    procrastinate --app=trackrecord.worker.app worker

Jobs are idempotent, so retries and duplicate runs are safe. Queueing locks keep
at most one pending job per company or filing.
"""

import logging
from dataclasses import dataclass
from functools import cache

import anthropic
import procrastinate
from procrastinate.exceptions import AlreadyEnqueued
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trackrecord.actuals.press import extract_reported_results
from trackrecord.config import Settings, get_settings
from trackrecord.db import make_engine, make_sessionmaker, session_scope
from trackrecord.edgar.client import EdgarClient
from trackrecord.ingest.runner import run_company_sync, run_document_fetch
from trackrecord.ingest.storage import RawStore
from trackrecord.ingest.sync import filings_missing_documents
from trackrecord.llm import Llm, has_llm_credentials, make_llm
from trackrecord.models import Company, Filing, FilingKind
from trackrecord.promises.extract import extract_filing
from trackrecord.resolve import resolve_company

log = logging.getLogger(__name__)

app = procrastinate.App(
    connector=procrastinate.PsycopgConnector(conninfo=get_settings().libpq_url),
    import_paths=["trackrecord.worker"],
)


@dataclass
class Resources:
    settings: Settings
    sessionmaker: async_sessionmaker[AsyncSession]
    client: EdgarClient
    store: RawStore
    llm: Llm | None  # None without LLM credentials: extraction is skipped


@cache
def resources() -> Resources:
    """Per-process resources, shared by all jobs so the EDGAR rate limit is shared too."""
    settings = get_settings()
    sessionmaker = make_sessionmaker(make_engine(settings.database_url))
    return Resources(
        settings=settings,
        sessionmaker=sessionmaker,
        client=EdgarClient(
            settings.sec_user_agent,
            max_requests_per_second=settings.sec_max_requests_per_second,
        ),
        store=RawStore(settings.raw_dir),
        llm=make_llm(sessionmaker, settings) if has_llm_credentials() else None,
    )


async def defer_company_sync(company_id: int) -> int | None:
    try:
        return await sync_company_task.configure(
            queueing_lock=f"sync-company-{company_id}"
        ).defer_async(company_id=company_id)
    except AlreadyEnqueued:
        return None


async def defer_extraction(filing_id: int) -> int | None:
    try:
        return await extract_promises_task.configure(
            queueing_lock=f"extract-promises-{filing_id}"
        ).defer_async(filing_id=filing_id)
    except AlreadyEnqueued:
        return None


async def defer_document_fetch(filing_id: int) -> int | None:
    try:
        return await fetch_documents_task.configure(
            queueing_lock=f"fetch-documents-{filing_id}"
        ).defer_async(filing_id=filing_id)
    except AlreadyEnqueued:
        return None


@app.task(
    name="sync_company",
    queue="sync",
    retry=procrastinate.RetryStrategy(max_attempts=3, exponential_wait=30),
)
async def sync_company_task(company_id: int) -> None:
    r = resources()
    stats = await run_company_sync(r.sessionmaker, r.client, r.settings, company_id)
    log.info("synced company %s: %s", company_id, stats)
    async with r.sessionmaker() as session:
        pending = await filings_missing_documents(session, company_id)
    for filing_id in pending:
        await defer_document_fetch(filing_id)


@app.task(
    name="fetch_documents",
    queue="documents",
    retry=procrastinate.RetryStrategy(max_attempts=5, exponential_wait=10),
)
async def fetch_documents_task(filing_id: int) -> None:
    r = resources()
    await run_document_fetch(r.sessionmaker, r.client, r.store, filing_id)
    if r.llm is not None and r.settings.auto_extract:
        async with r.sessionmaker() as session:
            filing = await session.get_one(Filing, filing_id)
        if filing.kind == FilingKind.EARNINGS_RELEASE:
            await defer_extraction(filing_id)


@app.task(
    name="extract_promises",
    queue="llm",
    # Budget errors are not retried: they persist until the next month or a higher cap.
    retry=procrastinate.RetryStrategy(
        max_attempts=3, exponential_wait=60, retry_exceptions={anthropic.APIError}
    ),
)
async def extract_promises_task(filing_id: int) -> None:
    r = resources()
    if r.llm is None:
        log.warning("no LLM credentials, skipping extraction of filing %s", filing_id)
        return
    stats = await extract_filing(
        r.sessionmaker,
        r.llm,
        r.store,
        filing_id,
        model=r.settings.extraction_model,
        effort=r.settings.extraction_effort,
    )
    results = await extract_reported_results(
        r.sessionmaker,
        r.llm,
        r.store,
        filing_id,
        model=r.settings.extraction_model,
        effort=r.settings.extraction_effort,
    )
    async with session_scope(r.sessionmaker) as session:
        filing = await session.get_one(Filing, filing_id)
        resolved = await resolve_company(session, filing.company_id)
    log.info("filing %s: promises %s, results %s, %s", filing_id, stats, results, resolved)


# EDGAR publishes new filings throughout the day; every two hours is plenty.
@app.periodic(cron="17 */2 * * *")
@app.task(name="sync_all", queue="sync")
async def sync_all_task(timestamp: int) -> None:
    async with resources().sessionmaker() as session:
        ids = (await session.scalars(select(Company.id).where(Company.active))).all()
    for company_id in ids:
        await defer_company_sync(company_id)
