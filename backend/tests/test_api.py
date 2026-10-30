from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.conftest import FakeEdgar
from tests.fakes import NVDA_Q2FY27_OUTPUT, FakeClaude, promise, response
from tests.test_resolve import add_promise
from trackrecord import worker
from trackrecord.api.app import create_app
from trackrecord.config import Settings
from trackrecord.ingest.runner import run_company_sync
from trackrecord.ingest.storage import RawStore
from trackrecord.ingest.sync import seed_companies
from trackrecord.llm import Llm
from trackrecord.models import Company
from trackrecord.promises.extract import extract_filing
from trackrecord.promises.schema import ExtractionOutput
from trackrecord.resolve import resolve_company

pytestmark = pytest.mark.db


@pytest.fixture
async def synced(
    sessionmaker: async_sessionmaker[AsyncSession], fake_edgar: FakeEdgar, settings: Settings
) -> None:
    async with sessionmaker() as session:
        await seed_companies(session, [{"ticker": "NVDA", "cik": 1045810}])
        await session.commit()
        company_id = (await session.scalars(select(Company.id))).one()
    async with fake_edgar.client() as client:
        await run_company_sync(sessionmaker, client, settings, company_id)


@asynccontextmanager
async def api_client(
    settings: Settings, *, open_queue: bool = False
) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(settings, open_queue=open_queue)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


@pytest.fixture
async def client(settings: Settings, synced: None) -> AsyncIterator[httpx.AsyncClient]:
    async with api_client(settings) as c:
        yield c


async def test_health(client: httpx.AsyncClient) -> None:
    response = await client.get("/health")
    assert response.json() == {"status": "ok", "database": True}


async def test_companies(client: httpx.AsyncClient) -> None:
    companies = (await client.get("/companies")).json()
    assert [c["ticker"] for c in companies] == ["NVDA"]

    company = (await client.get("/companies/nvda")).json()
    assert company["name"] == "NVIDIA CORP"
    assert (await client.get("/companies/XXXX")).status_code == 404


async def test_periods(client: httpx.AsyncClient) -> None:
    periods = (await client.get("/companies/NVDA/periods")).json()
    labels = [p["label"] for p in periods]
    # Ordered by end date, with the full year after the Q4 that ends on the same day.
    assert labels.index("FY2026 Q4") + 1 == labels.index("FY2026")
    assert periods[-1]["is_estimated"]


async def test_filings(client: httpx.AsyncClient) -> None:
    releases = (await client.get("/companies/NVDA/filings?kind=earnings_release")).json()
    assert len(releases) == 7
    assert releases[0]["accession"] == "0001045810-26-000073"
    assert releases[0]["period"]["label"] == "FY2027 Q2"

    assert len((await client.get("/companies/NVDA/filings?limit=3")).json()) == 3


async def test_quarterly_facts_latest_only(client: httpx.AsyncClient) -> None:
    response = await client.get(
        "/companies/NVDA/facts", params={"concept": "Revenues", "duration": "quarter"}
    )
    facts = response.json()
    periods = [(f["period_start"], f["period_end"]) for f in facts]
    assert len(periods) == len(set(periods)), "one value per period"
    q2 = next(f for f in facts if f["period_end"] == "2026-07-26")
    assert float(q2["value"]) == 96221000000


async def test_all_reported_values_when_not_latest_only(client: httpx.AsyncClient) -> None:
    latest = (await client.get("/companies/NVDA/facts", params={"concept": "Revenues"})).json()
    every = (
        await client.get(
            "/companies/NVDA/facts", params={"concept": "Revenues", "latest_only": "false"}
        )
    ).json()
    assert len(every) > len(latest)


async def test_sync_queues_one_job_per_company(settings: Settings, synced: None) -> None:
    async with api_client(settings, open_queue=True) as client:
        first = (await client.post("/sync", json={})).json()
        second = (await client.post("/sync", json={"tickers": ["nvda"]})).json()

    assert first == {"queued": ["NVDA"], "already_queued": []}
    assert second == {"queued": [], "already_queued": ["NVDA"]}
    assert worker.sync_company_task.name == "sync_company"


async def test_sync_runs(client: httpx.AsyncClient) -> None:
    runs = (await client.get("/sync/runs")).json()
    assert len(runs) == 1 and runs[0]["status"] == "succeeded"


async def test_token_is_required_when_configured(settings: Settings, synced: None) -> None:
    secured = settings.model_copy(update={"api_token": "s3cret"})
    async with api_client(secured) as client:
        assert (await client.get("/health")).status_code == 200
        assert (await client.get("/companies")).status_code == 401
        headers = {"Authorization": "Bearer wrong"}
        assert (await client.get("/companies", headers=headers)).status_code == 401
        headers = {"Authorization": "Bearer s3cret"}
        assert (await client.get("/companies", headers=headers)).status_code == 200


@pytest.fixture
async def extracted(
    sessionmaker: async_sessionmaker[AsyncSession], settings: Settings, release_id: int
) -> None:
    output = ExtractionOutput(
        promises=[*NVDA_Q2FY27_OUTPUT.promises, promise(metric="gross_margin", confidence=0.4)]
    )
    llm = Llm(FakeClaude(lambda kwargs: response(output)), sessionmaker, Decimal(30))
    await extract_filing(
        sessionmaker,
        llm,
        RawStore(settings.raw_dir),
        release_id,
        model="claude-haiku-5-5",
        effort="medium",
    )


async def test_promises_review_and_spend(settings: Settings, extracted: None) -> None:
    async with api_client(settings) as client:
        promises = (await client.get("/companies/NVDA/promises")).json()
        assert len(promises) == 7
        revenue = next(p for p in promises if p["metric_name"] == "Revenue")
        assert revenue["target_period"]["label"] == "FY2027 Q3"
        assert float(revenue["low"]) == 105.84e9

        queue = (await client.get("/review")).json()
        assert [p["review_reasons"] for p in queue] == [
            ["unit usd unexpected for gross_margin", "low confidence 0.40"]
        ]

        reviewed = await client.post(
            f"/promises/{queue[0]['id']}/review", json={"status": "rejected"}
        )
        assert reviewed.json()["review_status"] == "rejected"
        assert (await client.get("/review")).json() == []
        assert len((await client.get("/companies/NVDA/promises")).json()) == 6
        bad_status = await client.post(f"/promises/{queue[0]['id']}/review", json={"status": "x"})
        assert bad_status.status_code == 422
        assert (
            await client.post("/promises/999999/review", json={"status": "accepted"})
        ).status_code == 404

        spend = (await client.get("/llm/spend")).json()
        assert float(spend["month_to_date_usd"]) > 0
        assert float(spend["monthly_budget_usd"]) == 30


async def test_actuals_resolution_and_credibility(
    sessionmaker: async_sessionmaker[AsyncSession], settings: Settings, release_id: int
) -> None:
    async with sessionmaker() as session:
        promise = await add_promise(session, "FY2027 Q2")
        await resolve_company(session, promise.company_id, today=date(2026, 10, 1))
        await session.commit()

    async with api_client(settings) as client:
        actuals = (await client.get("/companies/NVDA/actuals", params={"metric": "revenue"})).json()
        q2 = next(a for a in actuals if a["period"]["label"] == "FY2027 Q2")
        assert (q2["source"], float(q2["value"])) == ("xbrl", 96221000000)

        (resolved,) = (await client.get("/companies/NVDA/promises")).json()
        assert resolved["resolution"]["outcome"] == "above"
        assert resolved["resolution"]["actual"]["period"]["label"] == "FY2027 Q2"

        credibility = (await client.get("/companies/NVDA/credibility")).json()
        assert (credibility["ticker"], credibility["resolved"], credibility["hits"]) == (
            "NVDA",
            1,
            1,
        )
        assert credibility["by_metric"][0]["metric"] == "revenue"
        ranking = (await client.get("/credibility")).json()
        assert [c["ticker"] for c in ranking] == ["NVDA"]
