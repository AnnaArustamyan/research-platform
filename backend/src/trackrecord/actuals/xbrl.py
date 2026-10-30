"""GAAP actuals from XBRL facts (docs/design.md §4.3, §5.3).

For each confirmed fiscal period, read the facts that cover exactly that period
and compute the dictionary metrics. Companies tag the same quantity with
different concepts, so each input has a list of concepts, tried in order.

Many companies file no 10-Q for Q4, so additive Q4 values are derived as the
fiscal year minus the nine months to the end of Q3. EPS can't be derived that
way (share counts differ), so Q4 EPS comes only from a filing that reports it.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from trackrecord.models import (
    Actual,
    ActualSource,
    Basis,
    Company,
    FiscalPeriod,
    Metric,
    Unit,
    XbrlFact,
)

INPUTS: dict[str, tuple[str, ...]] = {
    "revenue": (
        "Revenues",
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
        "SalesRevenueNet",
        "SalesRevenueGoodsNet",
    ),
    "gross_profit": ("GrossProfit",),
    "cost_of_revenue": ("CostOfRevenue", "CostOfGoodsAndServicesSold", "CostOfGoodsSold"),
    "operating_expenses": ("OperatingExpenses",),
    "operating_income": ("OperatingIncomeLoss",),
    "pretax_income": (
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments",
    ),
    "income_tax": ("IncomeTaxExpenseBenefit",),
    "eps_diluted": ("EarningsPerShareDiluted",),
}
CONCEPTS = {concept for concepts in INPUTS.values() for concept in concepts}
ADDITIVE = set(INPUTS) - {"eps_diluted"}

# Period starts derived from filing dates can differ from the XBRL contexts by a
# few days around 52/53-week year ends.
START_SLACK = timedelta(days=7)


@dataclass(frozen=True)
class Value:
    amount: Decimal
    concept: str
    accession: str
    filed_at: date
    derived: bool = False


@dataclass(frozen=True)
class ComputedActual:
    metric: Metric
    unit: Unit
    value: Decimal
    detail: str
    accession: str
    filed_at: date
    derived: bool


class FactIndex:
    """Latest-filed duration facts by (concept, end date)."""

    def __init__(self, facts: Iterable[XbrlFact]) -> None:
        self._by_end: dict[tuple[str, date], list[XbrlFact]] = {}
        for fact in facts:
            if fact.period_start is not None:
                self._by_end.setdefault((fact.concept, fact.period_end), []).append(fact)

    def find(self, concept: str, start: date, end: date) -> Value | None:
        candidates = [
            f
            for f in self._by_end.get((concept, end), [])
            if f.period_start is not None and abs(f.period_start - start) <= START_SLACK
        ]
        if not candidates:
            return None
        latest = max(candidates, key=lambda f: (f.filed_at, f.accession))
        return Value(latest.value, concept, latest.accession, latest.filed_at)


def read_input(
    index: FactIndex,
    name: str,
    period: FiscalPeriod,
    year: FiscalPeriod | None,
    q3: FiscalPeriod | None,
) -> Value | None:
    for concept in INPUTS[name]:
        value = index.find(concept, period.start_date, period.end_date)
        if value is not None:
            return value
    # Q4 of an additive quantity: the year minus the first nine months.
    if period.fiscal_quarter == 4 and name in ADDITIVE and year and q3:
        for concept in INPUTS[name]:
            full = index.find(concept, year.start_date, year.end_date)
            nine = index.find(concept, year.start_date, q3.end_date)
            if full and nine:
                return Value(
                    full.amount - nine.amount, concept, full.accession, full.filed_at, True
                )
    return None


def compute(inputs: dict[str, Value]) -> list[ComputedActual]:
    out: list[ComputedActual] = []

    def emit(metric: Metric, unit: Unit, value: Decimal, detail: str, *used: Value) -> None:
        newest = max(used, key=lambda v: v.filed_at)
        out.append(
            ComputedActual(
                metric=metric,
                unit=unit,
                value=value,
                detail=detail,
                accession=newest.accession,
                filed_at=newest.filed_at,
                derived=any(v.derived for v in used),
            )
        )

    def pct(numerator: Decimal, denominator: Decimal) -> Decimal:
        return (numerator / denominator * 100).quantize(Decimal("0.0001"))

    revenue = inputs.get("revenue")
    gross = inputs.get("gross_profit")
    cost = inputs.get("cost_of_revenue")
    opex = inputs.get("operating_expenses")
    operating = inputs.get("operating_income")
    pretax, tax, eps = (
        inputs.get("pretax_income"),
        inputs.get("income_tax"),
        inputs.get("eps_diluted"),
    )

    if revenue:
        emit(Metric.REVENUE, Unit.USD, revenue.amount, revenue.concept, revenue)
    if revenue and revenue.amount > 0:
        if gross:
            emit(
                Metric.GROSS_MARGIN,
                Unit.PERCENT,
                pct(gross.amount, revenue.amount),
                f"{gross.concept} / {revenue.concept}",
                gross,
                revenue,
            )
        elif cost:
            emit(
                Metric.GROSS_MARGIN,
                Unit.PERCENT,
                pct(revenue.amount - cost.amount, revenue.amount),
                f"({revenue.concept} - {cost.concept}) / {revenue.concept}",
                revenue,
                cost,
            )
        if operating:
            emit(
                Metric.OPERATING_MARGIN,
                Unit.PERCENT,
                pct(operating.amount, revenue.amount),
                f"{operating.concept} / {revenue.concept}",
                operating,
                revenue,
            )
    if opex:
        emit(Metric.OPERATING_EXPENSES, Unit.USD, opex.amount, opex.concept, opex)
    elif gross and operating:
        emit(
            Metric.OPERATING_EXPENSES,
            Unit.USD,
            gross.amount - operating.amount,
            f"{gross.concept} - {operating.concept}",
            gross,
            operating,
        )
    if pretax and tax and pretax.amount > 0:
        emit(
            Metric.TAX_RATE,
            Unit.PERCENT,
            pct(tax.amount, pretax.amount),
            f"{tax.concept} / {pretax.concept}",
            tax,
            pretax,
        )
    if eps:
        emit(Metric.EPS_DILUTED, Unit.USD_PER_SHARE, eps.amount, eps.concept, eps)
    return out


def _year_and_q3(
    period: FiscalPeriod, periods: Sequence[FiscalPeriod]
) -> tuple[FiscalPeriod | None, FiscalPeriod | None]:
    year = next(
        (p for p in periods if p.fiscal_year == period.fiscal_year and p.fiscal_quarter is None),
        None,
    )
    q3 = next(
        (p for p in periods if p.fiscal_year == period.fiscal_year and p.fiscal_quarter == 3),
        None,
    )
    return year, q3


async def derive_xbrl_actuals(session: AsyncSession, company: Company) -> int:
    """Compute and upsert GAAP actuals for every confirmed period. Returns the count."""
    periods = (
        await session.scalars(
            select(FiscalPeriod).where(
                FiscalPeriod.company_id == company.id, FiscalPeriod.is_estimated.is_(False)
            )
        )
    ).all()
    facts = (
        await session.scalars(
            select(XbrlFact).where(
                XbrlFact.company_id == company.id, XbrlFact.concept.in_(CONCEPTS)
            )
        )
    ).all()
    index = FactIndex(facts)

    rows = []
    for period in periods:
        year, q3 = _year_and_q3(period, periods)
        inputs = {
            name: value
            for name in INPUTS
            if (value := read_input(index, name, period, year, q3)) is not None
        }
        for actual in compute(inputs):
            rows.append(
                {
                    "company_id": company.id,
                    "period_id": period.id,
                    "metric": actual.metric,
                    "basis": Basis.GAAP,
                    "source": ActualSource.XBRL_DERIVED if actual.derived else ActualSource.XBRL,
                    "value": actual.value,
                    "unit": actual.unit,
                    "detail": actual.detail,
                    "accession": actual.accession,
                    "filed_at": actual.filed_at,
                }
            )
    if rows:
        stmt = insert(Actual).values(rows)
        stmt = stmt.on_conflict_do_update(
            index_elements=["period_id", "metric", "basis", "source"],
            set_={
                col: stmt.excluded[col]
                for col in ("value", "unit", "detail", "accession", "filed_at")
            }
            | {"updated_at": func.now()},
        )
        await session.execute(stmt)

    # Drop XBRL actuals no longer computed, e.g. a derived Q4 once a filing reports it.
    keep = {(r["period_id"], r["metric"], r["source"]) for r in rows}
    stale = (
        await session.scalars(
            select(Actual).where(
                Actual.company_id == company.id,
                Actual.source.in_([ActualSource.XBRL, ActualSource.XBRL_DERIVED]),
            )
        )
    ).all()
    for actual in stale:
        if (actual.period_id, actual.metric, actual.source) not in keep:
            await session.delete(actual)
    await session.flush()
    return len(rows)
