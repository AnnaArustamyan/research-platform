from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trackrecord.credibility import ResolvedPromise, all_credibility, shrunk_rate, summarize
from trackrecord.models import (
    Actual,
    ActualSource,
    Basis,
    Document,
    Filing,
    FiscalPeriod,
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
from trackrecord.promises.extract import refresh_supersession
from trackrecord.resolve import (
    choose_actual,
    expected_range,
    find_discrepancy,
    resolve_company,
    score,
)

D = Decimal


def make_promise(**overrides: object) -> Promise:
    fields: dict[str, object] = {
        "metric": Metric.REVENUE,
        "basis": Basis.UNSPECIFIED,
        "kind": PromiseKind.RANGE,
        "unit": Unit.USD,
        "low": D(100),
        "high": D(110),
        "approximate": False,
    }
    fields.update(overrides)
    return Promise(**fields)


def make_actual(source: ActualSource, value: str, **overrides: object) -> Actual:
    fields: dict[str, object] = {
        "metric": Metric.REVENUE,
        "basis": Basis.GAAP,
        "source": source,
        "value": D(value),
        "unit": Unit.USD,
    }
    fields.update(overrides)
    return Actual(**fields)


# --- Expected ranges --------------------------------------------------------------------


def test_point_tolerances() -> None:
    approx_usd = make_promise(kind=PromiseKind.POINT, low=D(1000), high=D(1000), approximate=True)
    assert expected_range(approx_usd, None) == (D(990), D(1010))

    exact_pct = make_promise(
        kind=PromiseKind.POINT, unit=Unit.PERCENT, low=D("40.7"), high=D("40.7")
    )
    assert expected_range(exact_pct, None) == (D("40.65"), D("40.75"))

    approx_pct = make_promise(
        kind=PromiseKind.POINT, unit=Unit.PERCENT, low=D(56), high=D(56), approximate=True
    )
    assert expected_range(approx_pct, None) == (D("55.5"), D("56.5"))

    eps = make_promise(
        kind=PromiseKind.POINT, unit=Unit.USD_PER_SHARE, low=D("0.13"), high=D("0.13")
    )
    assert expected_range(eps, None) == (D("0.125"), D("0.135"))


def test_relative_promise_uses_the_reference_actual() -> None:
    # "up sequentially between 7% and 9%"
    relative = make_promise(
        kind=PromiseKind.RELATIVE, low=None, high=None, change_low=D(7), change_high=D(9)
    )
    assert expected_range(relative, D(1000)) == (D(1070), D(1090))
    assert expected_range(relative, None) is None

    down = make_promise(
        kind=PromiseKind.RELATIVE, low=None, high=None, change_low=D(-5), change_high=D(-3)
    )
    assert expected_range(down, D(1000)) == (D(950), D(970))


def test_qualitative_promises_are_not_scored() -> None:
    assert expected_range(make_promise(kind=PromiseKind.QUALITATIVE), None) is None


# --- Scoring ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("actual", "outcome", "hit"),
    [
        ("99", Outcome.BELOW, False),
        ("101", Outcome.LOW_END, True),
        ("105", Outcome.MID, True),
        ("109", Outcome.HIGH_END, True),
        ("111", Outcome.ABOVE, True),
    ],
)
def test_range_outcomes(actual: str, outcome: Outcome, hit: bool) -> None:
    result = score(D(actual), D(100), D(110), metric=Metric.REVENUE, unit=Unit.USD, is_range=True)
    assert (result.outcome, result.hit) == (outcome, hit)


def test_surprise_is_signed_by_the_good_direction() -> None:
    revenue = score(D(115), D(100), D(110), metric=Metric.REVENUE, unit=Unit.USD, is_range=True)
    assert revenue.surprise_pct == D("9.5238")  # (115 - 105) / 105

    # Spending more than guided is a miss with a negative surprise.
    opex = score(
        D(115), D(100), D(110), metric=Metric.OPERATING_EXPENSES, unit=Unit.USD, is_range=True
    )
    assert (opex.outcome, opex.hit, opex.surprise_pct) == (Outcome.ABOVE, False, D("-9.5238"))


def test_percent_metrics_report_points() -> None:
    result = score(
        D("49.0"),
        D("46.9"),
        D("48.9"),
        metric=Metric.GROSS_MARGIN,
        unit=Unit.PERCENT,
        is_range=True,
    )
    assert result.outcome == Outcome.ABOVE
    assert result.surprise_pp == D("1.1000")


def test_bound_and_point_outcomes() -> None:
    at_least = score(D(95), D(100), None, metric=Metric.REVENUE, unit=Unit.USD, is_range=False)
    assert (at_least.outcome, at_least.hit) == (Outcome.BELOW, False)
    met = score(D(100), D(99), D(101), metric=Metric.REVENUE, unit=Unit.USD, is_range=False)
    assert met.outcome == Outcome.MET


# --- Choosing the actual ----------------------------------------------------------------


def test_gaap_prefers_xbrl_and_non_gaap_needs_the_release() -> None:
    actuals = [
        make_actual(ActualSource.PRESS_RELEASE, "106"),
        make_actual(ActualSource.XBRL_DERIVED, "105"),
        make_actual(ActualSource.PRESS_RELEASE, "107", basis=Basis.NON_GAAP),
    ]
    assert choose_actual(make_promise(), actuals) is actuals[1]
    margin = make_promise(metric=Metric.GROSS_MARGIN, basis=Basis.NON_GAAP)
    assert choose_actual(margin, actuals) is None
    gm = make_actual(
        ActualSource.PRESS_RELEASE, "60", metric=Metric.GROSS_MARGIN, basis=Basis.NON_GAAP
    )
    assert choose_actual(margin, [*actuals, gm]) is gm
    # Revenue is the same on any basis.
    assert choose_actual(make_promise(basis=Basis.NON_GAAP), actuals) is actuals[1]


def test_discrepancy_between_release_and_xbrl() -> None:
    xbrl = make_actual(ActualSource.XBRL, "1000")
    close = make_actual(ActualSource.PRESS_RELEASE, "1004")
    far = make_actual(ActualSource.PRESS_RELEASE, "1010")
    assert find_discrepancy(xbrl, [xbrl, close]) is None
    assert find_discrepancy(xbrl, [xbrl, far]) == "press release 1010 vs XBRL 1000"


# --- Against the database ---------------------------------------------------------------


async def add_promise(session: AsyncSession, period_label: str, **overrides: object) -> Promise:
    periods = {p.label: p for p in (await session.scalars(select(FiscalPeriod))).all()}
    filing = (await session.scalars(select(Filing).order_by(Filing.filed_at).limit(1))).one()
    document = (await session.scalars(select(Document).limit(1))).one()
    target = periods[period_label]
    fields: dict[str, object] = {
        "company_id": filing.company_id,
        "filing_id": filing.id,
        "document_id": document.id,
        "issued_at": date(2026, 5, 20),
        "target_fiscal_year": target.fiscal_year,
        "target_fiscal_quarter": target.fiscal_quarter,
        "target_period_id": target.id,
        "metric": Metric.REVENUE,
        "metric_name": "Revenue",
        "basis": Basis.UNSPECIFIED,
        "kind": PromiseKind.RANGE,
        "unit": Unit.USD,
        "low": D("90e9"),
        "high": D("94e9"),
        "quote": "q",
        "span_start": 0,
        "span_end": 1,
        "confidence": 1.0,
        "review_status": ReviewStatus.AUTO_ACCEPTED,
        "extractor_version": "test",
    }
    fields.update(overrides)
    promise = Promise(**fields)
    session.add(promise)
    await session.flush()
    return promise


@pytest.mark.db
async def test_resolve_against_xbrl_actuals(
    sessionmaker: async_sessionmaker[AsyncSession], release_id: int
) -> None:
    async with sessionmaker() as session:
        beat = await add_promise(session, "FY2027 Q2")
        future = await add_promise(session, "FY2027 Q3")
        non_gaap = await add_promise(
            session,
            "FY2027 Q2",
            metric=Metric.GROSS_MARGIN,
            basis=Basis.NON_GAAP,
            unit=Unit.PERCENT,
            low=D(74),
            high=D(76),
        )
        stats = await resolve_company(session, beat.company_id, today=date(2026, 10, 1))
        await session.commit()

        assert (stats.resolved, stats.waiting, stats.unresolvable) == (1, 2, 0)
        resolution = (await session.scalars(select(Resolution))).one()
        assert resolution.promise_id == beat.id
        assert resolution.actual_value == D(96221000000)
        assert (resolution.outcome, resolution.hit) == (Outcome.ABOVE, True)
        assert resolution.status == ResolutionStatus.CONFIRMED
        await session.refresh(beat)
        assert beat.status == PromiseStatus.RESOLVED
        await session.refresh(future)
        assert future.status == PromiseStatus.PENDING

        # A non-GAAP actual stays awaited until the release is read, however late.
        stats = await resolve_company(session, beat.company_id, today=date(2027, 12, 1))
        await session.refresh(non_gaap)
        assert non_gaap.status == PromiseStatus.PENDING


@pytest.mark.db
async def test_later_guidance_drops_an_earlier_resolution(
    sessionmaker: async_sessionmaker[AsyncSession], release_id: int
) -> None:
    async with sessionmaker() as session:
        first = await add_promise(session, "FY2027 Q2")
        await resolve_company(session, first.company_id, today=date(2026, 10, 1))
        assert (await session.scalars(select(Resolution))).one().promise_id == first.id

        # A mid-quarter update arrives later (e.g. from an out-of-order backfill).
        update = await add_promise(session, "FY2027 Q2", issued_at=date(2026, 6, 15))
        await refresh_supersession(session, first.company_id)
        await resolve_company(session, first.company_id, today=date(2026, 10, 1))
        await session.commit()

        await session.refresh(first)
        assert first.status == PromiseStatus.SUPERSEDED
        assert (await session.scalars(select(Resolution))).one().promise_id == update.id


# --- Credibility ------------------------------------------------------------------------


def resolved(
    hit: bool, end: str, outcome: Outcome = Outcome.MID, pct: str = "1"
) -> ResolvedPromise:
    return ResolvedPromise(
        company_id=1,
        metric=Metric.REVENUE,
        basis=Basis.GAAP,
        period_end=date.fromisoformat(end),
        hit=hit,
        outcome=outcome,
        surprise_pct=D(pct),
        surprise_pp=None,
    )


def test_shrinkage_pulls_small_samples_toward_the_sector() -> None:
    assert shrunk_rate(3, 3, prior=0.5) == pytest.approx(8 / 13)
    assert shrunk_rate(0, 0, prior=0.7) == pytest.approx(0.7)
    assert shrunk_rate(90, 100, prior=0.5) == pytest.approx(95 / 110)


def test_summarize() -> None:
    history = [
        resolved(True, "2025-03-31", Outcome.ABOVE, "3"),
        resolved(False, "2025-06-30", Outcome.BELOW, "-2"),
        resolved(True, "2025-09-30", Outcome.ABOVE, "4"),
        resolved(True, "2025-12-31", Outcome.HIGH_END, "1"),
    ]
    c = summarize(1, history, prior=0.5)

    assert (c.resolved, c.hits, c.hit_rate) == (4, 3, 0.75)
    assert c.score == round(100 * 8 / 14)
    assert c.beat_rate == 0.5
    assert c.streak == 2
    (metric,) = c.by_metric
    assert metric.mean_surprise_pct == pytest.approx(1.5)
    assert metric.stdev_surprise_pct is not None


def test_summarize_without_history() -> None:
    c = summarize(1, [], prior=0.5)
    assert (c.score, c.hit_rate, c.streak) == (None, None, 0)


@pytest.mark.db
async def test_all_credibility(
    sessionmaker: async_sessionmaker[AsyncSession], release_id: int
) -> None:
    async with sessionmaker() as session:
        promise = await add_promise(session, "FY2027 Q2")
        await resolve_company(session, promise.company_id, today=date(2026, 10, 1))
        await session.commit()
        (c,) = await all_credibility(session)
    assert (c.resolved, c.hits, c.score) == (1, 1, 100)  # the sector prior is this one hit
