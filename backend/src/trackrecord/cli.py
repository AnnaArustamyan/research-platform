"""Command-line entry point: `trackrecord --help`."""

import asyncio
import logging
from typing import Annotated

import typer
import uvicorn
from sqlalchemy import select

from trackrecord.config import get_settings
from trackrecord.db import make_engine, make_sessionmaker, session_scope
from trackrecord.edgar.client import EdgarClient
from trackrecord.ingest.checks import check_guidance
from trackrecord.ingest.runner import run_company_sync, run_document_fetch
from trackrecord.ingest.storage import RawStore
from trackrecord.ingest.sync import filings_missing_documents, load_universe, seed_companies
from trackrecord.models import Company

cli = typer.Typer(no_args_is_help=True, help="Track Record backend tools.")


@cli.callback()
def main(verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


@cli.command()
def seed() -> None:
    """Load the company universe into the database."""

    async def run() -> None:
        settings = get_settings()
        engine = make_engine(settings.database_url)
        async with session_scope(make_sessionmaker(engine)) as session:
            count = await seed_companies(session, load_universe())
        await engine.dispose()
        typer.echo(f"seeded {count} companies")

    asyncio.run(run())


@cli.command()
def sync(
    tickers: Annotated[
        list[str] | None, typer.Argument(help="Default: all active companies")
    ] = None,
    documents: Annotated[bool, typer.Option(help="Also download documents")] = True,
) -> None:
    """Sync companies from EDGAR in this process (no worker needed)."""

    async def run() -> None:
        settings = get_settings()
        engine = make_engine(settings.database_url)
        sessionmaker = make_sessionmaker(engine)
        store = RawStore(settings.raw_dir)
        async with EdgarClient(
            settings.sec_user_agent,
            max_requests_per_second=settings.sec_max_requests_per_second,
        ) as client:
            async with sessionmaker() as session:
                stmt = select(Company).where(Company.active).order_by(Company.ticker)
                if tickers:
                    stmt = stmt.where(Company.ticker.in_([t.upper() for t in tickers]))
                companies = list((await session.scalars(stmt)).all())
            for company in companies:
                stats = await run_company_sync(sessionmaker, client, settings, company.id)
                warnings = stats.pop("calendar_warnings")
                typer.echo(f"{company.ticker}: {stats}")
                for warning in warnings:
                    typer.echo(f"  warning: {warning}")
                if documents:
                    async with sessionmaker() as session:
                        pending = await filings_missing_documents(session, company.id)
                    for filing_id in pending:
                        await run_document_fetch(sessionmaker, client, store, filing_id)
                    typer.echo(f"  fetched documents for {len(pending)} filings")
        await engine.dispose()

    asyncio.run(run())


@cli.command("check-guidance")
def check_guidance_command() -> None:
    """Flag which companies publish quarterly guidance (needs synced documents)."""

    async def run() -> None:
        settings = get_settings()
        engine = make_engine(settings.database_url)
        store = RawStore(settings.raw_dir)
        async with session_scope(make_sessionmaker(engine)) as session:
            companies = (await session.scalars(select(Company).order_by(Company.ticker))).all()
            for company in companies:
                result = await check_guidance(session, store, company)
                typer.echo(
                    f"{result.ticker:6} guidance in {result.releases_with_guidance}"
                    f"/{result.releases_checked} recent releases -> {result.guides_quarterly}"
                )
        await engine.dispose()

    asyncio.run(run())


@cli.command()
def api(host: str = "127.0.0.1", port: int = 8000, reload: bool = False) -> None:
    """Run the HTTP API."""
    uvicorn.run("trackrecord.api.main:app", host=host, port=port, reload=reload, log_level="info")
