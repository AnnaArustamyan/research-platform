"""Command-line entry point: `trackrecord --help`."""

import asyncio
import json
import logging
from pathlib import Path
from typing import Annotated

import anthropic
import typer
import uvicorn
from sqlalchemy import Select, select

from trackrecord.actuals.press import extract_reported_results, releases_without_results
from trackrecord.config import get_settings
from trackrecord.credibility import all_credibility
from trackrecord.db import make_engine, make_sessionmaker, session_scope
from trackrecord.edgar.client import EdgarClient
from trackrecord.evals.extraction import golden_filing_ids, load_golden, score_database
from trackrecord.ingest.checks import check_guidance
from trackrecord.ingest.runner import (
    refresh_company_results,
    run_company_sync,
    run_document_fetch,
)
from trackrecord.ingest.storage import RawStore
from trackrecord.ingest.sync import filings_missing_documents, load_universe, seed_companies
from trackrecord.llm import has_claude_credentials, make_llm, month_spend
from trackrecord.models import Company
from trackrecord.promises.batch import collect as collect_batches
from trackrecord.promises.batch import submit as submit_batch
from trackrecord.promises.extract import ExtractionStats, extract_filing, releases_to_extract

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


def _companies_stmt(tickers: list[str] | None) -> Select[int]:
    stmt = select(Company.id).where(Company.active)
    if tickers:
        stmt = stmt.where(Company.ticker.in_([t.upper() for t in tickers]))
    return stmt


@cli.command()
def extract(
    tickers: Annotated[
        list[str] | None, typer.Argument(help="Default: all active companies")
    ] = None,
    limit: Annotated[int | None, typer.Option(help="At most this many releases")] = None,
    batch: Annotated[bool, typer.Option(help="Use the Batch API (50% cheaper, async)")] = False,
) -> None:
    """Extract promises from earnings releases not yet processed by this extractor."""

    async def run() -> None:
        settings = get_settings()
        if not has_claude_credentials():
            raise typer.BadParameter("set ANTHROPIC_API_KEY to extract promises")
        engine = make_engine(settings.database_url)
        sessionmaker = make_sessionmaker(engine)
        store = RawStore(settings.raw_dir)
        async with sessionmaker() as session:
            company_ids = list((await session.scalars(_companies_stmt(tickers))).all())
            filing_ids = await releases_to_extract(session, company_ids)
        filing_ids = filing_ids[:limit] if limit else filing_ids
        typer.echo(f"{len(filing_ids)} releases to extract")

        if batch:
            submitted = await submit_batch(
                sessionmaker,
                anthropic.AsyncAnthropic(),
                store,
                filing_ids,
                model=settings.extraction_model,
                effort=settings.extraction_effort,
                monthly_budget_usd=settings.llm_monthly_budget_usd,
            )
            typer.echo(
                f"submitted batch {submitted.batch_id}: {submitted.requests} requests, "
                f"~${submitted.estimated_cost_usd:.2f}. Run `trackrecord collect` later."
            )
        else:
            llm = make_llm(sessionmaker, settings)
            total = ExtractionStats()
            for filing_id in filing_ids:
                total.add(
                    await extract_filing(
                        sessionmaker,
                        llm,
                        store,
                        filing_id,
                        model=settings.extraction_model,
                        effort=settings.extraction_effort,
                    )
                )
            _echo_stats(total)
        await engine.dispose()

    asyncio.run(run())


@cli.command()
def collect() -> None:
    """Store the results of finished extraction batches."""

    async def run() -> None:
        settings = get_settings()
        engine = make_engine(settings.database_url)
        result = await collect_batches(make_sessionmaker(engine), anthropic.AsyncAnthropic())
        typer.echo(
            f"collected {result.batches} batches ({result.still_running} still running, "
            f"{result.failed_requests} failed requests)"
        )
        if result.stats:
            _echo_stats(result.stats)
        await engine.dispose()

    asyncio.run(run())


def _echo_stats(stats: ExtractionStats) -> None:
    typer.echo(
        f"extracted {stats.extracted}, stored {stats.stored}, duplicates {stats.duplicates}, "
        f"needs review {stats.needs_review}, rejected {len(stats.rejected)}, "
        f"targets other than the next quarter {stats.not_next_quarter}"
    )
    for reason in stats.rejected:
        typer.echo(f"  rejected: {reason}")


@cli.command("read-results")
def read_results(
    tickers: Annotated[
        list[str] | None, typer.Argument(help="Default: all active companies")
    ] = None,
    limit: Annotated[int | None, typer.Option(help="At most this many releases")] = None,
) -> None:
    """Extract reported results (incl. non-GAAP) from earnings releases, then resolve."""

    async def run() -> None:
        settings = get_settings()
        if not has_claude_credentials():
            raise typer.BadParameter("set ANTHROPIC_API_KEY to read results")
        engine = make_engine(settings.database_url)
        sessionmaker = make_sessionmaker(engine)
        llm = make_llm(sessionmaker, settings)
        async with sessionmaker() as session:
            company_ids = list((await session.scalars(_companies_stmt(tickers))).all())
            filing_ids = await releases_without_results(session, company_ids)
        filing_ids = filing_ids[:limit] if limit else filing_ids
        typer.echo(f"{len(filing_ids)} releases to read")
        stored = 0
        for filing_id in filing_ids:
            stats = await extract_reported_results(
                sessionmaker,
                llm,
                RawStore(settings.raw_dir),
                filing_id,
                model=settings.extraction_model,
                effort=settings.extraction_effort,
            )
            stored += stats.stored
        typer.echo(f"stored {stored} reported values")
        for company_id in company_ids:
            await refresh_company_results(sessionmaker, company_id)
        await engine.dispose()

    asyncio.run(run())


@cli.command()
def resolve(
    tickers: Annotated[
        list[str] | None, typer.Argument(help="Default: all active companies")
    ] = None,
) -> None:
    """Recompute GAAP actuals from XBRL and resolve promises (no API calls)."""

    async def run() -> None:
        settings = get_settings()
        engine = make_engine(settings.database_url)
        sessionmaker = make_sessionmaker(engine)
        async with sessionmaker() as session:
            companies = (
                await session.scalars(
                    select(Company)
                    .where(Company.id.in_(_companies_stmt(tickers)))
                    .order_by(Company.ticker)
                )
            ).all()
        for company in companies:
            stats = await refresh_company_results(sessionmaker, company.id)
            typer.echo(f"{company.ticker}: {stats}")
        await engine.dispose()

    asyncio.run(run())


@cli.command()
def credibility() -> None:
    """Credibility scores of all companies, best first."""

    async def run() -> None:
        settings = get_settings()
        engine = make_engine(settings.database_url)
        async with make_sessionmaker(engine)() as session:
            tickers = dict((await session.execute(select(Company.id, Company.ticker))).all())
            scores = await all_credibility(session)
        await engine.dispose()
        for c in sorted(scores, key=lambda c: (c.score is None, -(c.score or 0))):
            if c.resolved == 0:
                typer.echo(f"{tickers[c.company_id]:5}   -  no resolved promises")
                continue
            typer.echo(
                f"{tickers[c.company_id]:5} {c.score:3}  hits {c.hits}/{c.resolved}, "
                f"beat rate {c.beat_rate:.0%}, streak {c.streak:+d}"
            )

    asyncio.run(run())


@cli.command("eval-extraction")
def eval_extraction(
    run: Annotated[
        bool, typer.Option(help="Re-extract the golden filings first (costs tokens)")
    ] = False,
    output: Annotated[Path | None, typer.Option(help="Write the full report as JSON")] = None,
) -> None:
    """Score extracted promises against evals/extraction/golden.yaml."""

    async def main() -> None:
        settings = get_settings()
        engine = make_engine(settings.database_url)
        sessionmaker = make_sessionmaker(engine)
        golden = load_golden()
        if run:
            if not has_claude_credentials():
                raise typer.BadParameter("set ANTHROPIC_API_KEY to run the extractor")
            llm = make_llm(sessionmaker, settings)
            async with sessionmaker() as session:
                filing_ids = await golden_filing_ids(session, golden)
            for filing_id in filing_ids:
                await extract_filing(
                    sessionmaker,
                    llm,
                    RawStore(settings.raw_dir),
                    filing_id,
                    model=settings.extraction_model,
                    effort=settings.extraction_effort,
                    force=True,
                )
        async with sessionmaker() as session:
            report = await score_database(session, golden)
        await engine.dispose()

        for f in report.filings:
            problems = [
                *(f"missing {x}" for x in f.false_negatives),
                *(f"extra {x}" for x in f.false_positives),
                *(f"wrong value {x}" for x in f.value_errors),
                *([f.calendar_error] if f.calendar_error else []),
            ]
            typer.echo(f"{f.ticker:5} {f.accession}: {'ok' if not problems else ''}")
            for problem in problems:
                typer.echo(f"    {problem}")
        typer.echo(json.dumps(report.summary()))
        if output:
            output.write_text(json.dumps(report.to_dict(), indent=2))

    asyncio.run(main())


@cli.command()
def spend() -> None:
    """LLM spend this month against the budget."""

    async def run() -> None:
        settings = get_settings()
        engine = make_engine(settings.database_url)
        async with make_sessionmaker(engine)() as session:
            spent = await month_spend(session)
        await engine.dispose()
        typer.echo(f"${spent:.4f} of ${settings.llm_monthly_budget_usd} this month")

    asyncio.run(run())


@cli.command()
def api(host: str = "127.0.0.1", port: int = 8000, reload: bool = False) -> None:
    """Run the HTTP API."""
    uvicorn.run("trackrecord.api.main:app", host=host, port=port, reload=reload, log_level="info")
