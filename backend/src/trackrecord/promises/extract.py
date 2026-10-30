"""Promise extraction for earnings releases (docs/design.md §5.2).

    documents --text--> model --ExtractionOutput--> normalize/validate --> promises

Applying an extraction is separate from requesting it, so the same code stores
results from a direct call and from the Batch API.
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from trackrecord.db import session_scope
from trackrecord.documents import PARSER_VERSION, html_to_text
from trackrecord.ingest.storage import RawStore
from trackrecord.llm import Llm
from trackrecord.models import (
    Company,
    Document,
    Filing,
    FilingKind,
    FiscalPeriod,
    LlmCall,
    LlmCallStatus,
    Metric,
    Promise,
    PromiseStatus,
    ReviewStatus,
)
from trackrecord.promises.normalize import NormalizedPromise, Rejected, normalize
from trackrecord.promises.prompt import EXTRACTOR_VERSION, SYSTEM_PROMPT, user_message
from trackrecord.promises.schema import ExtractionOutput
from trackrecord.resolve import drop_resolutions

log = logging.getLogger(__name__)

TASK = f"extract_promises:{EXTRACTOR_VERSION}"


@dataclass
class ExtractionStats:
    extracted: int = 0
    stored: int = 0
    duplicates: int = 0
    needs_review: int = 0
    rejected: list[str] = field(default_factory=list)
    not_next_quarter: int = 0

    def add(self, other: "ExtractionStats") -> None:
        self.extracted += other.extracted
        self.stored += other.stored
        self.duplicates += other.duplicates
        self.needs_review += other.needs_review
        self.rejected += other.rejected
        self.not_next_quarter += other.not_next_quarter


def subject(document: Document) -> str:
    return f"document:{document.id}"


async def release_documents(
    session: AsyncSession, store: RawStore, filing: Filing
) -> list[Document]:
    """The filing's press-release exhibits, with text parsed and saved if needed."""
    documents = (
        await session.scalars(
            select(Document)
            .where(Document.filing_id == filing.id, Document.doc_type.startswith("EX-99"))
            .order_by(Document.filename)
        )
    ).all()
    for document in documents:
        if document.storage_path and document.parser_version != PARSER_VERSION:
            document.text = html_to_text(store.read(document.storage_path))
            document.parser_version = PARSER_VERSION
            document.parsed_at = datetime.now(UTC)
    return [d for d in documents if d.text]


def _label(period: FiscalPeriod | None) -> str | None:
    return period.label if period else None


def next_quarter(period: FiscalPeriod, periods: Sequence[FiscalPeriod]) -> FiscalPeriod | None:
    quarters = sorted((p for p in periods if p.fiscal_quarter), key=lambda p: p.end_date)
    later = [q for q in quarters if q.end_date > period.end_date]
    return later[0] if later else None


@dataclass
class FilingContext:
    company: Company
    filing: Filing
    reported: FiscalPeriod | None
    next: FiscalPeriod | None
    periods: Sequence[FiscalPeriod]


async def load_context(session: AsyncSession, filing: Filing) -> FilingContext:
    company = await session.get_one(Company, filing.company_id)
    periods = (
        await session.scalars(select(FiscalPeriod).where(FiscalPeriod.company_id == company.id))
    ).all()
    reported = next((p for p in periods if p.id == filing.period_id), None)
    return FilingContext(
        company=company,
        filing=filing,
        reported=reported,
        next=next_quarter(reported, periods) if reported else None,
        periods=periods,
    )


def build_user_message(ctx: FilingContext, document: Document) -> str:
    assert document.text is not None
    return user_message(
        company=ctx.company.name or ctx.company.ticker,
        ticker=ctx.company.ticker,
        filed_at=ctx.filing.filed_at,
        reported_period=_label(ctx.reported),
        next_period=_label(ctx.next),
        document_text=document.text,
    )


def _is_future(p: NormalizedPromise, reported: FiscalPeriod) -> bool:
    if p.fiscal_quarter is None:
        return p.fiscal_year >= reported.fiscal_year
    assert reported.fiscal_quarter is not None
    return (p.fiscal_year, p.fiscal_quarter) > (reported.fiscal_year, reported.fiscal_quarter)


async def apply_extraction(
    session: AsyncSession,
    ctx: FilingContext,
    document: Document,
    output: ExtractionOutput,
    llm_call_id: int | None,
) -> ExtractionStats:
    """Validate and store one document's extraction, replacing earlier versions."""
    assert document.text is not None
    stats = ExtractionStats(extracted=len(output.promises))
    await session.execute(delete(Promise).where(Promise.document_id == document.id))

    existing = (
        await session.scalars(
            select(Promise).where(
                Promise.filing_id == ctx.filing.id, Promise.document_id != document.id
            )
        )
    ).all()
    seen = {_stored_key(p) for p in existing}
    period_ids = {(p.fiscal_year, p.fiscal_quarter): p.id for p in ctx.periods}

    for extracted in output.promises:
        try:
            normalized = normalize(extracted, document.text)
            if ctx.reported and not _is_future(normalized, ctx.reported):
                raise Rejected(f"not a future period: {_period_text(normalized)}")
        except Rejected as exc:
            stats.rejected.append(f"{extracted.metric_name}: {exc}")
            continue

        key = normalized.dedup_key()
        if key in seen:
            stats.duplicates += 1
            continue
        seen.add(key)

        reasons = list(normalized.review_reasons)
        if ctx.reported is None:
            reasons.append("reported period unknown")
        target_id = period_ids.get((normalized.fiscal_year, normalized.fiscal_quarter))
        if target_id is None:
            reasons.append(f"{_period_text(normalized)} not in calendar")
        if (
            normalized.fiscal_quarter
            and ctx.next
            and (normalized.fiscal_year, normalized.fiscal_quarter)
            != (ctx.next.fiscal_year, ctx.next.fiscal_quarter)
        ):
            stats.not_next_quarter += 1

        session.add(
            Promise(
                company_id=ctx.company.id,
                filing_id=ctx.filing.id,
                document_id=document.id,
                issued_at=ctx.filing.filed_at,
                issuing_period_id=ctx.reported.id if ctx.reported else None,
                target_fiscal_year=normalized.fiscal_year,
                target_fiscal_quarter=normalized.fiscal_quarter,
                target_period_id=target_id,
                metric=normalized.metric,
                metric_name=normalized.metric_name,
                basis=normalized.basis,
                kind=normalized.kind,
                unit=normalized.unit,
                low=normalized.low,
                high=normalized.high,
                approximate=normalized.approximate,
                relative_reference=normalized.relative_reference,
                change_low=normalized.change_low,
                change_high=normalized.change_high,
                interpretation=normalized.interpretation,
                quote=normalized.quote,
                span_start=normalized.span_start,
                span_end=normalized.span_end,
                confidence=normalized.confidence,
                review_status=ReviewStatus.NEEDS_REVIEW if reasons else ReviewStatus.AUTO_ACCEPTED,
                review_reasons=reasons,
                extractor_version=EXTRACTOR_VERSION,
                llm_call_id=llm_call_id,
            )
        )
        stats.stored += 1
        stats.needs_review += bool(reasons)

    await session.flush()
    await refresh_supersession(session, ctx.company.id)
    return stats


def _period_text(p: NormalizedPromise) -> str:
    return f"FY{p.fiscal_year}" + (f" Q{p.fiscal_quarter}" if p.fiscal_quarter else "")


def _stored_key(p: Promise) -> tuple[object, ...]:
    name = p.metric_name.lower() if p.metric == Metric.OTHER else ""
    return (p.metric, name, p.basis, p.target_fiscal_year, p.target_fiscal_quarter)


async def refresh_supersession(session: AsyncSession, company_id: int) -> None:
    """For each metric, basis and target period, the latest guidance stands and earlier
    guidance is superseded. Order-independent, so batch results can arrive in any order,
    even after an earlier promise was resolved: its resolution is then dropped."""
    promises = (
        await session.scalars(
            select(Promise)
            .where(Promise.company_id == company_id)
            .order_by(Promise.issued_at, Promise.id)
        )
    ).all()
    standing = [p for p in promises if p.review_status != ReviewStatus.REJECTED]
    latest: dict[tuple[object, ...], Promise] = {}
    for promise in standing:
        latest[_stored_key(promise)] = promise  # ordered, so the last one wins

    unresolved: list[int] = [p.id for p in promises if p.review_status == ReviewStatus.REJECTED]
    for promise in standing:
        winner = latest[_stored_key(promise)]
        if promise is winner:
            if promise.status == PromiseStatus.SUPERSEDED:
                promise.status = PromiseStatus.PENDING
            promise.superseded_by_id = None
        else:
            if promise.status in {PromiseStatus.RESOLVED, PromiseStatus.UNRESOLVABLE}:
                unresolved.append(promise.id)
            promise.status, promise.superseded_by_id = PromiseStatus.SUPERSEDED, winner.id
    for promise in promises:
        if (
            promise.review_status == ReviewStatus.REJECTED
            and promise.status == PromiseStatus.RESOLVED
        ):
            promise.status = PromiseStatus.PENDING
    await drop_resolutions(session, unresolved)


async def already_extracted(session: AsyncSession, document: Document) -> bool:
    call = await session.scalar(
        select(LlmCall.id).where(
            LlmCall.task == TASK,
            LlmCall.subject == subject(document),
            LlmCall.status == LlmCallStatus.SUCCEEDED,
        )
    )
    return call is not None


async def extract_filing(
    sessionmaker: async_sessionmaker[AsyncSession],
    llm: Llm,
    store: RawStore,
    filing_id: int,
    *,
    model: str,
    effort: str,
    force: bool = False,
) -> ExtractionStats:
    """Extract promises from every press-release exhibit of one earnings release."""
    total = ExtractionStats()
    async with session_scope(sessionmaker) as session:
        filing = await session.get_one(Filing, filing_id)
        if filing.kind != FilingKind.EARNINGS_RELEASE:
            return total
        documents = await release_documents(session, store, filing)
        pending = [d for d in documents if force or not await already_extracted(session, d)]
        ctx = await load_context(session, filing)
        requests = [(d, build_user_message(ctx, d)) for d in pending]

    for document, message in requests:
        result = await llm.parse(
            task=TASK,
            subject=subject(document),
            model=model,
            system=SYSTEM_PROMPT,
            user=message,
            output_format=ExtractionOutput,
            effort=effort,
        )
        async with session_scope(sessionmaker) as session:
            filing = await session.get_one(Filing, filing_id)
            ctx = await load_context(session, filing)
            document = await session.get_one(Document, document.id)
            stats = await apply_extraction(session, ctx, document, result.output, result.call_id)
        log.info("%s %s: %s", filing.accession, document.filename, stats)
        total.add(stats)
    return total


async def releases_to_extract(
    session: AsyncSession, company_ids: Sequence[int] | None = None
) -> list[int]:
    """Earnings releases with an exhibit that the current extractor hasn't processed."""
    extracted = select(LlmCall.id).where(
        LlmCall.task == TASK,
        LlmCall.status == LlmCallStatus.SUCCEEDED,
        LlmCall.subject == func.concat("document:", Document.id),
    )
    pending_document = select(Document.id).where(
        Document.filing_id == Filing.id,
        Document.doc_type.startswith("EX-99"),
        ~extracted.exists(),
    )
    stmt = (
        select(Filing.id)
        .where(Filing.kind == FilingKind.EARNINGS_RELEASE, pending_document.exists())
        .order_by(Filing.filed_at)
    )
    if company_ids is not None:
        stmt = stmt.where(Filing.company_id.in_(company_ids))
    return list((await session.scalars(stmt)).all())


async def mark_reviewed(
    session: AsyncSession, promise_id: int, status: ReviewStatus
) -> Promise | None:
    await session.execute(
        update(Promise).where(Promise.id == promise_id).values(review_status=status)
    )
    promise = await session.scalar(
        select(Promise).where(Promise.id == promise_id).options(selectinload(Promise.target_period))
    )
    if promise is not None:
        await refresh_supersession(session, promise.company_id)
    return promise
