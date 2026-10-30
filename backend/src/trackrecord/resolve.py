"""Resolve promises against actuals, deterministically (docs/design.md §5.3).

Which actual a promise is compared with:
- GAAP (or unstated basis): XBRL, else XBRL-derived Q4, else the press release
  (provisional until XBRL arrives);
- non-GAAP: the press release;
- revenue: any basis, since companies rarely adjust it.

Ranges are compared as given. A point is widened by a tolerance: ±1% for
"approximately" amounts (±0.5 pp for percentages), otherwise rounding only.
Relative promises are turned into a range from the reference period's actual.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from trackrecord.models import (
    Actual,
    ActualSource,
    Basis,
    Document,
    Filing,
    FilingKind,
    FiscalPeriod,
    LlmCall,
    LlmCallStatus,
    Metric,
    Outcome,
    Promise,
    PromiseKind,
    PromiseStatus,
    Resolution,
    ResolutionStatus,
    ReviewStatus,
    Unit,
)

LOWER_IS_BETTER = {Metric.OPERATING_EXPENSES, Metric.TAX_RATE}

APPROX_RELATIVE = Decimal("0.01")
APPROX_PP = Decimal("0.5")
ROUNDING_RELATIVE = Decimal("0.005")
ROUNDING_PP = Decimal("0.05")
EPS_MIN_TOLERANCE = Decimal("0.01")
# Press release and XBRL GAAP values further apart than this are flagged.
DISCREPANCY_RELATIVE = Decimal("0.005")
DISCREPANCY_PP = Decimal("0.15")
# Give up waiting for an actual this long after the period ends.
UNRESOLVABLE_AFTER = timedelta(days=150)

GAAP_ORDER = (ActualSource.XBRL, ActualSource.XBRL_DERIVED, ActualSource.PRESS_RELEASE)


def tolerance(value: Decimal, unit: Unit | None, approximate: bool) -> Decimal:
    if unit == Unit.PERCENT:
        return APPROX_PP if approximate else ROUNDING_PP
    delta = abs(value) * (APPROX_RELATIVE if approximate else ROUNDING_RELATIVE)
    if unit == Unit.USD_PER_SHARE:
        return max(delta, EPS_MIN_TOLERANCE if approximate else EPS_MIN_TOLERANCE / 2)
    return delta


def expected_range(
    promise: Promise, reference: Decimal | None
) -> tuple[Decimal | None, Decimal | None] | None:
    """The range an actual must fall in, or None if it can't be computed."""
    match promise.kind:
        case PromiseKind.RANGE | PromiseKind.BOUND:
            return promise.low, promise.high
        case PromiseKind.POINT:
            assert promise.low is not None
            delta = tolerance(promise.low, promise.unit, promise.approximate)
            return promise.low - delta, promise.low + delta
        case PromiseKind.RELATIVE:
            if reference is None or promise.change_low is None or promise.change_high is None:
                return None
            if promise.unit == Unit.PERCENT:  # a change in a percentage is in points
                return reference + promise.change_low, reference + promise.change_high
            ends = sorted(
                reference * (1 + c / 100) for c in (promise.change_low, promise.change_high)
            )
            return ends[0], ends[1]
        case PromiseKind.QUALITATIVE:
            return None


@dataclass(frozen=True)
class Scored:
    outcome: Outcome
    hit: bool
    surprise_pct: Decimal | None
    surprise_pp: Decimal | None


def score(
    actual: Decimal,
    low: Decimal | None,
    high: Decimal | None,
    *,
    metric: Metric,
    unit: Unit | None,
    is_range: bool,
) -> Scored:
    higher_is_better = metric not in LOWER_IS_BETTER
    if low is not None and actual < low:
        outcome = Outcome.BELOW
    elif high is not None and actual > high:
        outcome = Outcome.ABOVE
    elif is_range and low is not None and high is not None and high > low:
        position = (actual - low) / (high - low)
        outcome = (
            Outcome.LOW_END
            if position < Decimal(1) / 3
            else Outcome.HIGH_END
            if position > Decimal(2) / 3
            else Outcome.MID
        )
    else:
        outcome = Outcome.MET

    beyond_good = Outcome.ABOVE if higher_is_better else Outcome.BELOW
    hit = outcome not in {Outcome.ABOVE, Outcome.BELOW} or outcome == beyond_good

    reference = (
        (low + high) / 2
        if low is not None and high is not None
        else low
        if low is not None
        else high
    )
    sign = 1 if higher_is_better else -1
    surprise_pct = surprise_pp = None
    if reference is not None:
        if reference != 0:
            surprise_pct = (sign * (actual - reference) / abs(reference) * 100).quantize(
                Decimal("0.0001")
            )
        if unit == Unit.PERCENT:
            surprise_pp = (sign * (actual - reference)).quantize(Decimal("0.0001"))
    return Scored(outcome, hit, surprise_pct, surprise_pp)


def choose_actual(promise: Promise, actuals: Sequence[Actual]) -> Actual | None:
    """The best actual for the promise's period, metric and basis."""
    candidates = [a for a in actuals if a.metric == promise.metric]
    if promise.basis == Basis.NON_GAAP and promise.metric != Metric.REVENUE:
        return next(
            (a for a in candidates if a.basis == Basis.NON_GAAP),
            None,
        )
    gaap = [a for a in candidates if a.basis in {Basis.GAAP, Basis.UNSPECIFIED}]
    for source in GAAP_ORDER:
        found = next((a for a in gaap if a.source == source), None)
        if found:
            return found
    if promise.metric == Metric.REVENUE:  # revenue reported only as non-GAAP
        return next(iter(candidates), None)
    return None


def find_discrepancy(chosen: Actual, actuals: Sequence[Actual]) -> str | None:
    if chosen.source == ActualSource.PRESS_RELEASE:
        return None
    reported = next(
        (
            a
            for a in actuals
            if a.metric == chosen.metric
            and a.basis in {Basis.GAAP, Basis.UNSPECIFIED}
            and a.source == ActualSource.PRESS_RELEASE
        ),
        None,
    )
    if reported is None:
        return None
    difference = abs(reported.value - chosen.value)
    limit = (
        DISCREPANCY_PP if chosen.unit == Unit.PERCENT else abs(chosen.value) * DISCREPANCY_RELATIVE
    )
    if difference <= limit:
        return None
    return f"press release {reported.value} vs XBRL {chosen.value}"


def reference_period(
    promise: Promise, target: FiscalPeriod, periods: Sequence[FiscalPeriod]
) -> FiscalPeriod | None:
    quarters = sorted((p for p in periods if p.fiscal_quarter), key=lambda p: p.end_date)
    match promise.relative_reference:
        case "prior_quarter":
            earlier = [q for q in quarters if q.end_date < target.end_date]
            return earlier[-1] if earlier else None
        case "prior_year_quarter":
            return next(
                (
                    p
                    for p in periods
                    if p.fiscal_year == target.fiscal_year - 1
                    and p.fiscal_quarter == target.fiscal_quarter
                ),
                None,
            )
        case "prior_year":
            return next(
                (
                    p
                    for p in periods
                    if p.fiscal_year == target.fiscal_year - 1 and p.fiscal_quarter is None
                ),
                None,
            )
    return None


@dataclass
class ResolveStats:
    resolved: int = 0
    waiting: int = 0
    unresolvable: int = 0


async def resolve_company(
    session: AsyncSession, company_id: int, today: date | None = None
) -> ResolveStats:
    """(Re)resolve every standing promise of a company whose target period has ended."""
    today = today or datetime.now(UTC).date()
    stats = ResolveStats()
    periods = (
        await session.scalars(select(FiscalPeriod).where(FiscalPeriod.company_id == company_id))
    ).all()
    by_id = {p.id: p for p in periods}
    actuals_by_period: dict[int, list[Actual]] = {}
    for actual in (
        await session.scalars(select(Actual).where(Actual.company_id == company_id))
    ).all():
        actuals_by_period.setdefault(actual.period_id, []).append(actual)

    promises = (
        await session.scalars(
            select(Promise).where(
                Promise.company_id == company_id,
                Promise.status.in_(
                    [PromiseStatus.PENDING, PromiseStatus.RESOLVED, PromiseStatus.UNRESOLVABLE]
                ),
                Promise.review_status != ReviewStatus.REJECTED,
                Promise.metric != Metric.OTHER,
                Promise.kind != PromiseKind.QUALITATIVE,
                Promise.target_period_id.is_not(None),
            )
        )
    ).all()
    releases_read = await periods_with_results_read(session, company_id)
    existing = {
        r.promise_id: r
        for r in (
            await session.scalars(
                select(Resolution).where(Resolution.promise_id.in_([p.id for p in promises]))
            )
        ).all()
    }

    for promise in promises:
        assert promise.target_period_id is not None
        target = by_id[promise.target_period_id]
        if target.is_estimated or target.end_date >= today:
            stats.waiting += 1
            continue
        actuals = actuals_by_period.get(target.id, [])
        chosen = choose_actual(promise, actuals)

        reference = None
        if promise.kind == PromiseKind.RELATIVE:
            ref_period = reference_period(promise, target, periods)
            ref_actual = (
                choose_actual(promise, actuals_by_period.get(ref_period.id, []))
                if ref_period
                else None
            )
            reference = ref_actual.value if ref_actual else None

        expected = expected_range(promise, reference)
        if chosen is None or expected is None:
            # Give up only once the release reporting the period has been read too:
            # until then a non-GAAP or Q4 EPS actual may still arrive from it.
            if today - target.end_date > UNRESOLVABLE_AFTER and target.id in releases_read:
                promise.status = PromiseStatus.UNRESOLVABLE
                stats.unresolvable += 1
            else:
                stats.waiting += 1
            if promise.id in existing:
                await session.delete(existing[promise.id])
            continue

        low, high = expected
        result = score(
            chosen.value,
            low,
            high,
            metric=promise.metric,
            unit=promise.unit,
            is_range=promise.kind in {PromiseKind.RANGE, PromiseKind.RELATIVE},
        )
        resolution = existing.get(promise.id) or Resolution(promise_id=promise.id)
        resolution.actual_id = chosen.id
        resolution.actual_value = chosen.value
        resolution.expected_low = low
        resolution.expected_high = high
        resolution.outcome = result.outcome
        resolution.hit = result.hit
        resolution.surprise_pct = result.surprise_pct
        resolution.surprise_pp = result.surprise_pp
        resolution.status = (
            ResolutionStatus.PROVISIONAL
            if chosen.source == ActualSource.PRESS_RELEASE
            else ResolutionStatus.CONFIRMED
        )
        resolution.discrepancy = find_discrepancy(chosen, actuals)
        resolution.resolved_at = datetime.now(UTC)
        session.add(resolution)
        promise.status = PromiseStatus.RESOLVED
        stats.resolved += 1

    await session.flush()
    return stats


async def periods_with_results_read(session: AsyncSession, company_id: int) -> set[int]:
    """Periods whose earnings release has been through results extraction."""
    read = select(LlmCall.id).where(
        LlmCall.task.startswith("extract_actuals:"),
        LlmCall.status == LlmCallStatus.SUCCEEDED,
        LlmCall.subject == func.concat("document:", Document.id),
    )
    stmt = (
        select(Filing.period_id)
        .join(Document, Document.filing_id == Filing.id)
        .where(
            Filing.company_id == company_id,
            Filing.kind == FilingKind.EARNINGS_RELEASE,
            Filing.period_id.is_not(None),
            read.exists(),
        )
    )
    return {pid for pid in (await session.scalars(stmt)).all() if pid is not None}


async def drop_resolutions(session: AsyncSession, promise_ids: Sequence[int]) -> None:
    if promise_ids:
        await session.execute(delete(Resolution).where(Resolution.promise_id.in_(promise_ids)))
