from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.conftest import load_fixture
from tests.fakes import FakeLlmClient, response
from trackrecord.actuals.press import (
    ReportedResults,
    ReportedValue,
    check,
    extract_reported_results,
    releases_without_results,
    to_actuals,
)
from trackrecord.actuals.xbrl import Value, compute, derive_xbrl_actuals
from trackrecord.config import Settings
from trackrecord.documents import html_to_text
from trackrecord.ingest.storage import RawStore
from trackrecord.llm import Llm
from trackrecord.models import (
    Actual,
    ActualSource,
    Basis,
    Company,
    FiscalPeriod,
    Metric,
    Unit,
)


def value(amount: str, concept: str = "C", derived: bool = False) -> Value:
    return Value(Decimal(amount), concept, "acc", date(2026, 1, 1), derived)


def test_compute_margins_and_fallbacks() -> None:
    computed = {
        a.metric: a
        for a in compute(
            {
                "revenue": value("1000", "Revenues"),
                "cost_of_revenue": value("400", "CostOfRevenue"),
                "operating_income": value("250", "OperatingIncomeLoss"),
                "pretax_income": value("200"),
                "income_tax": value("30"),
                "eps_diluted": value("1.25"),
            }
        )
    }
    # No GrossProfit: revenue minus cost of revenue.
    assert computed[Metric.GROSS_MARGIN].value == Decimal("60.0000")
    assert computed[Metric.GROSS_MARGIN].detail == "(Revenues - CostOfRevenue) / Revenues"
    assert computed[Metric.OPERATING_MARGIN].value == Decimal("25.0000")
    assert computed[Metric.TAX_RATE].value == Decimal("15.0000")
    assert computed[Metric.EPS_DILUTED].unit == Unit.USD_PER_SHARE
    # Operating expenses need gross profit and operating income, or their own concept.
    assert Metric.OPERATING_EXPENSES not in computed


def test_compute_opex_from_gross_profit_and_skips_meaningless_ratios() -> None:
    computed = {
        a.metric: a
        for a in compute(
            {
                "revenue": value("1000"),
                "gross_profit": value("600", "GrossProfit", derived=True),
                "operating_income": value("250", "OperatingIncomeLoss"),
                "pretax_income": value("-10"),  # a loss: no tax rate
                "income_tax": value("5"),
            }
        )
    }
    assert computed[Metric.OPERATING_EXPENSES].value == Decimal(350)
    assert computed[Metric.OPERATING_EXPENSES].derived
    assert Metric.TAX_RATE not in computed


async def periods(session: AsyncSession) -> dict[str, FiscalPeriod]:
    return {p.label: p for p in (await session.scalars(select(FiscalPeriod))).all()}


@pytest.mark.db
async def test_derive_from_real_facts_including_q4(
    sessionmaker: async_sessionmaker[AsyncSession], release_id: int
) -> None:
    async with sessionmaker() as session:
        # The sync in the release_id fixture already derived them.
        by_period = await periods(session)
        actuals = {
            (a.period_id, a.metric): a for a in (await session.scalars(select(Actual))).all()
        }

    q2 = by_period["FY2027 Q2"].id
    assert actuals[(q2, Metric.REVENUE)].value == Decimal(96221000000)
    assert actuals[(q2, Metric.REVENUE)].source == ActualSource.XBRL
    assert actuals[(q2, Metric.GROSS_MARGIN)].value == Decimal("74.9753")
    assert actuals[(q2, Metric.EPS_DILUTED)].value == Decimal("2.46")

    # Q4 FY2026 has no 10-Q: fiscal year minus the nine months to Q3.
    q4 = by_period["FY2026 Q4"].id
    assert actuals[(q4, Metric.REVENUE)].value == Decimal(68127000000)
    assert actuals[(q4, Metric.REVENUE)].source == ActualSource.XBRL_DERIVED
    assert actuals[(q4, Metric.OPERATING_EXPENSES)].value == Decimal(6794000000)
    assert (q4, Metric.EPS_DILUTED) not in actuals  # EPS can't be subtracted

    year = by_period["FY2026"].id
    assert actuals[(year, Metric.EPS_DILUTED)].value == Decimal("4.9")


@pytest.mark.db
async def test_derive_is_idempotent(
    sessionmaker: async_sessionmaker[AsyncSession], release_id: int
) -> None:
    async with sessionmaker() as session:
        company = (await session.scalars(select(Company))).one()
        first = await derive_xbrl_actuals(session, company)
        await session.commit()
        ids = set((await session.scalars(select(Actual.id))).all())
        second = await derive_xbrl_actuals(session, company)
        await session.commit()
        assert first == second
        assert set((await session.scalars(select(Actual.id))).all()) == ids


# --- Press releases --------------------------------------------------------------------

TEXT = html_to_text(load_fixture("nvda_q2fy27_release.htm"))


def reported(**overrides: object) -> ReportedValue:
    fields: dict[str, object] = {
        "metric": "revenue",
        "basis": "gaap",
        "fiscal_year": 2027,
        "fiscal_quarter": 2,
        "value": 96221,
        "scale": "millions",
        "unit": "usd",
        "quote": "Revenue | $96,221 | $81,615 | 18%",
    }
    fields.update(overrides)
    return ReportedValue.model_validate(fields)


def test_check_scales_values_and_verifies_quotes() -> None:
    checked = check(reported(), TEXT)
    assert not isinstance(checked, str)
    assert checked.value == Decimal(96221000000)

    assert check(reported(quote="Revenue | $1"), TEXT) == "quote not found"
    assert check(reported(value=96222), TEXT) == "96222.0 not in quote"


def test_margins_are_computed_from_amounts_when_not_stated() -> None:
    values = []
    for metric, amount in [("revenue", 1000), ("operating_income", 400)]:
        item = check(
            reported(
                metric=metric,
                basis="non_gaap",
                value=amount,
                quote=f"{metric} {amount}",
            ),
            f"{metric} {amount}",
        )
        assert not isinstance(item, str)
        values.append(item)

    actuals = {metric: (v, detail) for metric, v, detail in to_actuals(values)}
    margin, detail = actuals[Metric.OPERATING_MARGIN]
    assert margin.value == Decimal("40.0000")
    assert margin.basis == Basis.NON_GAAP
    assert detail == "operating_income / revenue"


RESULTS = ReportedResults(
    values=[
        reported(),
        reported(
            metric="gross_margin",
            value=75.0,
            scale="units",
            unit="percent",
            quote="Gross margin | 75.0% | 74.9% | 0.1 pts",
        ),
        reported(fiscal_quarter=1, value=81615),  # the prior quarter: not reported here
    ]
)


@pytest.mark.db
async def test_extract_reported_results(
    sessionmaker: async_sessionmaker[AsyncSession], settings: Settings, release_id: int
) -> None:
    client = FakeLlmClient(lambda kwargs: response(RESULTS))
    llm = Llm(client, sessionmaker, Decimal(30))

    stats = await extract_reported_results(
        sessionmaker,
        llm,
        RawStore(settings.raw_dir),
        release_id,
        model="claude-haiku-5-5",
        effort="low",
    )

    assert len(client.messages.calls) == 2  # two exhibits
    assert stats.stored == 2  # the second exhibit repeats the same values
    assert stats.rejected == ["revenue: period (2027, 1) is not reported here"] * 2
    async with sessionmaker() as session:
        stored = (
            await session.scalars(
                select(Actual)
                .where(Actual.source == ActualSource.PRESS_RELEASE)
                .order_by(Actual.metric)
            )
        ).all()
        assert [(a.metric, a.source, a.value) for a in stored] == [
            (Metric.REVENUE, ActualSource.PRESS_RELEASE, Decimal(96221000000)),
            (Metric.GROSS_MARGIN, ActualSource.PRESS_RELEASE, Decimal("75.0")),
        ]
        assert stored[0].quote == "Revenue | $96,221 | $81,615 | 18%"
        assert release_id not in await releases_without_results(session)
