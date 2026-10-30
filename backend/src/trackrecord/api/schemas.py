from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from trackrecord.models import (
    Basis,
    FilingKind,
    Metric,
    PromiseKind,
    PromiseStatus,
    ReviewStatus,
    SyncStatus,
    Unit,
)


class Schema(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class Health(Schema):
    status: str
    database: bool


class CompanyOut(Schema):
    id: int
    ticker: str
    cik: int
    name: str | None
    fiscal_year_end: str | None
    active: bool
    guides_quarterly: bool | None
    last_synced_at: datetime | None


class FiscalPeriodOut(Schema):
    id: int
    fiscal_year: int
    fiscal_quarter: int | None
    label: str
    start_date: date
    end_date: date
    is_estimated: bool


class DocumentOut(Schema):
    id: int
    doc_type: str
    filename: str
    description: str | None
    url: str
    size_bytes: int | None
    fetched_at: datetime | None


class FilingOut(Schema):
    id: int
    accession: str
    form: str
    kind: FilingKind
    is_amendment: bool
    filed_at: date
    report_date: date | None
    items: list[str]
    period: FiscalPeriodOut | None
    documents: list[DocumentOut]


class FactOut(Schema):
    concept: str
    unit: str
    value: Decimal
    period_start: date | None
    period_end: date
    accession: str
    form: str
    filed_at: date


class SyncRunOut(Schema):
    id: int
    company_id: int
    status: SyncStatus
    started_at: datetime
    finished_at: datetime | None
    stats: dict[str, Any]
    error: str | None


class SyncRequest(BaseModel):
    tickers: list[str] | None = None  # None means every active company


class SyncQueued(BaseModel):
    queued: list[str]
    already_queued: list[str]


class PromiseOut(Schema):
    id: int
    company_id: int
    filing_id: int
    document_id: int
    issued_at: date
    target_fiscal_year: int
    target_fiscal_quarter: int | None
    target_period: FiscalPeriodOut | None
    metric: Metric
    metric_name: str
    basis: Basis
    kind: PromiseKind
    unit: Unit | None
    low: Decimal | None
    high: Decimal | None
    approximate: bool
    relative_reference: str | None
    change_low: Decimal | None
    change_high: Decimal | None
    interpretation: str | None
    quote: str
    span_start: int
    span_end: int
    confidence: float
    status: PromiseStatus
    review_status: ReviewStatus
    review_reasons: list[str]
    superseded_by_id: int | None


class ReviewRequest(BaseModel):
    status: Literal[ReviewStatus.ACCEPTED, ReviewStatus.REJECTED]


class SpendOut(BaseModel):
    month_to_date_usd: Decimal
    monthly_budget_usd: Decimal
