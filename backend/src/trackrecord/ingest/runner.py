"""Run sync steps with their own sessions and record each run in `sync_runs`.

Shared by the CLI and the background worker.
"""

from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trackrecord.actuals.xbrl import derive_xbrl_actuals
from trackrecord.config import Settings
from trackrecord.db import session_scope
from trackrecord.edgar.client import EdgarClient
from trackrecord.ingest.storage import RawStore
from trackrecord.ingest.sync import fetch_filing_documents, sync_company
from trackrecord.models import Company, Filing, SyncRun, SyncStatus
from trackrecord.resolve import resolve_company


async def run_company_sync(
    sessionmaker: async_sessionmaker[AsyncSession],
    client: EdgarClient,
    settings: Settings,
    company_id: int,
) -> dict[str, Any]:
    async with session_scope(sessionmaker) as session:
        run = SyncRun(company_id=company_id, status=SyncStatus.RUNNING)
        session.add(run)
        await session.flush()
        run_id = run.id

    try:
        async with session_scope(sessionmaker) as session:
            company = await session.get_one(Company, company_id)
            stats = await sync_company(session, client, company, settings)
        stats |= await refresh_company_results(sessionmaker, company_id)
    except Exception as exc:
        await _finish_run(sessionmaker, run_id, SyncStatus.FAILED, error=repr(exc))
        raise
    await _finish_run(sessionmaker, run_id, SyncStatus.SUCCEEDED, stats=stats)
    return stats


async def refresh_company_results(
    sessionmaker: async_sessionmaker[AsyncSession], company_id: int
) -> dict[str, Any]:
    """Recompute GAAP actuals from XBRL and re-resolve the company's promises."""
    async with session_scope(sessionmaker) as session:
        company = await session.get_one(Company, company_id)
        actuals = await derive_xbrl_actuals(session, company)
        resolved = await resolve_company(session, company_id)
    return {"xbrl_actuals": actuals, "resolve": asdict(resolved)}


async def _finish_run(
    sessionmaker: async_sessionmaker[AsyncSession],
    run_id: int,
    status: SyncStatus,
    *,
    stats: dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    async with session_scope(sessionmaker) as session:
        await session.execute(
            update(SyncRun)
            .where(SyncRun.id == run_id)
            .values(status=status, finished_at=datetime.now(UTC), stats=stats or {}, error=error)
        )


async def run_document_fetch(
    sessionmaker: async_sessionmaker[AsyncSession],
    client: EdgarClient,
    store: RawStore,
    filing_id: int,
) -> int:
    async with session_scope(sessionmaker) as session:
        filing = await session.get_one(Filing, filing_id)
        if filing.documents_fetched_at is not None:
            return 0
        company = await session.get_one(Company, filing.company_id)
        return await fetch_filing_documents(session, client, store, filing, company.cik)
