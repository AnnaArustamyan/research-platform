import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from psycopg_pool import AsyncConnectionPool
from sqlalchemy import Select, func, select, text
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from trackrecord import worker
from trackrecord.api.schemas import (
    CompanyOut,
    FactOut,
    FilingOut,
    FiscalPeriodOut,
    Health,
    PromiseOut,
    ReviewRequest,
    SpendOut,
    SyncQueued,
    SyncRequest,
    SyncRunOut,
)
from trackrecord.config import Settings, get_settings
from trackrecord.db import make_engine, make_sessionmaker
from trackrecord.llm import month_spend
from trackrecord.models import (
    Company,
    Filing,
    FilingKind,
    FiscalPeriod,
    Metric,
    Promise,
    PromiseStatus,
    ReviewStatus,
    SyncRun,
    XbrlFact,
)
from trackrecord.promises.extract import mark_reviewed

bearer = HTTPBearer(auto_error=False)


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.sessionmaker() as session:
        yield session


async def check_token(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> None:
    expected: str | None = request.app.state.settings.api_token
    if expected is None:
        return
    if credentials is None or not secrets.compare_digest(credentials.credentials, expected):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or missing API token")


SessionDep = Annotated[AsyncSession, Depends(get_session)]

public = APIRouter()
router = APIRouter(dependencies=[Depends(check_token)])


async def company_by_ticker(session: AsyncSession, ticker: str) -> Company:
    company = await session.scalar(select(Company).where(Company.ticker == ticker.upper()))
    if company is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown ticker {ticker!r}")
    return company


@public.get("/health", response_model=Health)
async def health(session: SessionDep) -> Health:
    try:
        await session.execute(text("SELECT 1"))
        return Health(status="ok", database=True)
    except Exception:
        return Health(status="degraded", database=False)


@router.get("/companies", response_model=list[CompanyOut])
async def list_companies(session: SessionDep) -> list[Company]:
    return list((await session.scalars(select(Company).order_by(Company.ticker))).all())


@router.get("/companies/{ticker}", response_model=CompanyOut)
async def get_company(ticker: str, session: SessionDep) -> Company:
    return await company_by_ticker(session, ticker)


@router.get("/companies/{ticker}/periods", response_model=list[FiscalPeriodOut])
async def list_periods(ticker: str, session: SessionDep) -> list[FiscalPeriod]:
    company = await company_by_ticker(session, ticker)
    stmt = (
        select(FiscalPeriod)
        .where(FiscalPeriod.company_id == company.id)
        .order_by(FiscalPeriod.end_date, FiscalPeriod.fiscal_quarter.nulls_last())
    )
    return list((await session.scalars(stmt)).all())


@router.get("/companies/{ticker}/filings", response_model=list[FilingOut])
async def list_filings(
    ticker: str,
    session: SessionDep,
    kind: FilingKind | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[Filing]:
    company = await company_by_ticker(session, ticker)
    stmt = (
        select(Filing)
        .where(Filing.company_id == company.id)
        .options(selectinload(Filing.period), selectinload(Filing.documents))
        .order_by(Filing.filed_at.desc())
        .limit(limit)
    )
    if kind is not None:
        stmt = stmt.where(Filing.kind == kind)
    return list((await session.scalars(stmt)).all())


@router.get("/companies/{ticker}/facts", response_model=list[FactOut])
async def list_facts(
    ticker: str,
    session: SessionDep,
    concept: str,
    duration: Literal["quarter", "year", "instant", "any"] = "any",
    latest_only: bool = True,
) -> list[XbrlFact]:
    """Values of one XBRL concept. With `latest_only`, each period shows the most
    recently filed value, which reflects any restatement."""
    company = await company_by_ticker(session, ticker)
    stmt = select(XbrlFact).where(XbrlFact.company_id == company.id, XbrlFact.concept == concept)
    days = XbrlFact.period_end - XbrlFact.period_start
    if duration == "quarter":
        stmt = stmt.where(days.between(80, 100))
    elif duration == "year":
        stmt = stmt.where(days.between(350, 380))
    elif duration == "instant":
        stmt = stmt.where(XbrlFact.period_start.is_(None))
    if latest_only:
        stmt = stmt.ext(
            distinct_on(XbrlFact.unit, XbrlFact.period_start, XbrlFact.period_end)
        ).order_by(
            XbrlFact.unit,
            XbrlFact.period_start,
            XbrlFact.period_end,
            XbrlFact.filed_at.desc(),
        )
    facts = (await session.scalars(stmt)).all()
    return sorted(facts, key=lambda f: (f.period_end, f.period_start or f.period_end))


def _promises_query() -> Select[Promise]:
    return select(Promise).options(selectinload(Promise.target_period))


@router.get("/companies/{ticker}/promises", response_model=list[PromiseOut])
async def list_promises(
    ticker: str,
    session: SessionDep,
    metric: Metric | None = None,
    status: PromiseStatus | None = None,
    include_rejected: bool = False,
) -> list[Promise]:
    company = await company_by_ticker(session, ticker)
    stmt = (
        _promises_query()
        .where(Promise.company_id == company.id)
        .order_by(Promise.issued_at.desc(), Promise.metric, Promise.basis)
    )
    if metric is not None:
        stmt = stmt.where(Promise.metric == metric)
    if status is not None:
        stmt = stmt.where(Promise.status == status)
    if not include_rejected:
        stmt = stmt.where(Promise.review_status != ReviewStatus.REJECTED)
    return list((await session.scalars(stmt)).all())


@router.get("/review", response_model=list[PromiseOut])
async def review_queue(
    session: SessionDep, limit: Annotated[int, Query(ge=1, le=500)] = 100
) -> list[Promise]:
    """Promises the extractor wasn't sure about, oldest first."""
    stmt = (
        _promises_query()
        .where(Promise.review_status == ReviewStatus.NEEDS_REVIEW)
        .order_by(Promise.issued_at, Promise.id)
        .limit(limit)
    )
    return list((await session.scalars(stmt)).all())


@router.post("/promises/{promise_id}/review", response_model=PromiseOut)
async def review_promise(promise_id: int, request: ReviewRequest, session: SessionDep) -> Promise:
    promise = await mark_reviewed(session, promise_id, request.status)
    if promise is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown promise {promise_id}")
    await session.commit()
    await session.refresh(promise, ["target_period"])
    return promise


@router.get("/llm/spend", response_model=SpendOut)
async def llm_spend(request: Request, session: SessionDep) -> SpendOut:
    return SpendOut(
        month_to_date_usd=await month_spend(session),
        monthly_budget_usd=request.app.state.settings.llm_monthly_budget_usd,
    )


@router.post("/sync", response_model=SyncQueued)
async def queue_sync(request: SyncRequest, session: SessionDep) -> SyncQueued:
    stmt = select(Company).where(Company.active)
    if request.tickers is not None:
        stmt = stmt.where(Company.ticker.in_([t.upper() for t in request.tickers]))
    queued, already = [], []
    for company in (await session.scalars(stmt)).all():
        job_id = await worker.defer_company_sync(company.id)
        (queued if job_id is not None else already).append(company.ticker)
    return SyncQueued(queued=queued, already_queued=already)


@router.get("/sync/runs", response_model=list[SyncRunOut])
async def list_sync_runs(
    session: SessionDep, limit: Annotated[int, Query(ge=1, le=500)] = 50
) -> list[SyncRun]:
    stmt = select(SyncRun).order_by(SyncRun.started_at.desc()).limit(limit)
    return list((await session.scalars(stmt)).all())


@router.get("/stats")
async def stats(session: SessionDep) -> dict[str, int]:
    async def count(model: type) -> int:
        return await session.scalar(select(func.count()).select_from(model)) or 0

    return {
        "companies": await count(Company),
        "filings": await count(Filing),
        "fiscal_periods": await count(FiscalPeriod),
        "xbrl_facts": await count(XbrlFact),
        "promises": await count(Promise),
    }


def create_app(settings: Settings | None = None, *, open_queue: bool = True) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = make_engine(settings.database_url)
        app.state.settings = settings
        app.state.sessionmaker = make_sessionmaker(engine)
        if open_queue:
            # Jobs go to this app's database, whatever the worker module was configured with.
            pool = AsyncConnectionPool(settings.libpq_url, open=False, min_size=1, max_size=4)
            async with pool, worker.app.open_async(pool=pool):
                yield
        else:
            yield
        await engine.dispose()

    app = FastAPI(title="Track Record API", version="0.1.0", lifespan=lifespan)
    app.include_router(public)
    app.include_router(router)
    return app
