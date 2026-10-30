"""Database schema. See docs/design.md §4.4.

Later milestones add their own tables (promises, chunks, ...) in new migrations.
"""

import enum
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    false,
    func,
    true,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def _enum(cls: type[enum.Enum], name: str) -> Enum:
    return Enum(cls, name=name, values_callable=lambda e: [m.value for m in e])


class FilingKind(enum.StrEnum):
    EARNINGS_RELEASE = "earnings_release"  # 8-K with Item 2.02
    QUARTERLY_REPORT = "quarterly_report"  # 10-Q
    ANNUAL_REPORT = "annual_report"  # 10-K


class SyncStatus(enum.StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class Company(Base):
    __tablename__ = "companies"

    id: Mapped[int] = mapped_column(primary_key=True)
    cik: Mapped[int] = mapped_column(Integer, unique=True)
    ticker: Mapped[str] = mapped_column(String(12), unique=True)
    name: Mapped[str | None] = mapped_column(Text)
    sic_description: Mapped[str | None] = mapped_column(Text)
    fiscal_year_end: Mapped[str | None] = mapped_column(String(4))  # MMDD, as EDGAR reports it
    active: Mapped[bool] = mapped_column(server_default=true())
    guides_quarterly: Mapped[bool | None] = mapped_column()  # set by the M1/M2 data checks
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    filings: Mapped[list["Filing"]] = relationship(back_populates="company")


class FiscalPeriod(Base):
    """A company-specific fiscal quarter (1-4) or full year (quarter is NULL)."""

    __tablename__ = "fiscal_periods"
    __table_args__ = (
        UniqueConstraint(
            "company_id", "fiscal_year", "fiscal_quarter", postgresql_nulls_not_distinct=True
        ),
        CheckConstraint("fiscal_quarter BETWEEN 1 AND 4", name="fiscal_quarter_range"),
        CheckConstraint("start_date <= end_date", name="period_dates_ordered"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"))
    fiscal_year: Mapped[int]
    fiscal_quarter: Mapped[int | None]
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)
    # True until a filing confirms the dates (projected future periods, or the first
    # period in history whose start can't be derived from a previous period).
    is_estimated: Mapped[bool] = mapped_column(server_default=false())

    @property
    def label(self) -> str:
        return f"FY{self.fiscal_year}" + (f" Q{self.fiscal_quarter}" if self.fiscal_quarter else "")


class Filing(Base):
    __tablename__ = "filings"
    __table_args__ = (Index("ix_filings_company_filed", "company_id", "filed_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"))
    accession: Mapped[str] = mapped_column(String(20), unique=True)
    form: Mapped[str] = mapped_column(String(16))
    kind: Mapped[FilingKind] = mapped_column(_enum(FilingKind, "filing_kind"))
    is_amendment: Mapped[bool] = mapped_column(server_default=false())
    filed_at: Mapped[date] = mapped_column(Date)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    report_date: Mapped[date | None] = mapped_column(Date)
    items: Mapped[list[str]] = mapped_column(ARRAY(String(8)), server_default="{}")
    primary_document: Mapped[str | None] = mapped_column(Text)
    period_id: Mapped[int | None] = mapped_column(
        ForeignKey("fiscal_periods.id", ondelete="SET NULL")
    )
    documents_fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    company: Mapped[Company] = relationship(back_populates="filings")
    period: Mapped[FiscalPeriod | None] = relationship()
    documents: Mapped[list["Document"]] = relationship(back_populates="filing")


class Document(Base):
    """One file inside a filing: a press release exhibit or a report's main document."""

    __tablename__ = "documents"
    __table_args__ = (UniqueConstraint("filing_id", "filename"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    filing_id: Mapped[int] = mapped_column(ForeignKey("filings.id", ondelete="CASCADE"))
    doc_type: Mapped[str] = mapped_column(String(32))  # e.g. "EX-99.1", "10-Q"
    filename: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    url: Mapped[str] = mapped_column(Text)
    size_bytes: Mapped[int | None]
    sha256: Mapped[str | None] = mapped_column(String(64))
    storage_path: Mapped[str | None] = mapped_column(Text)  # relative to the raw data dir
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Filled by the parsers in later milestones.
    text: Mapped[str | None] = mapped_column(Text)
    parser_version: Mapped[str | None] = mapped_column(String(32))
    parsed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    filing: Mapped[Filing] = relationship(back_populates="documents")


class XbrlFact(Base):
    """One reported XBRL value, as published in SEC's companyfacts API.

    The same value appears once per filing that reports it (e.g. as a prior-year
    comparative), so `accession` is part of the key and keeps provenance. `fiscal_year`
    and `fiscal_period` describe the *filing*, not the fact's own period.
    """

    __tablename__ = "xbrl_facts"
    __table_args__ = (
        UniqueConstraint(
            "company_id",
            "taxonomy",
            "concept",
            "unit",
            "period_start",
            "period_end",
            "accession",
            postgresql_nulls_not_distinct=True,
        ),
        Index("ix_xbrl_facts_lookup", "company_id", "concept", "period_end"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"))
    taxonomy: Mapped[str] = mapped_column(String(16))
    concept: Mapped[str] = mapped_column(Text)
    unit: Mapped[str] = mapped_column(String(32))
    value: Mapped[Decimal] = mapped_column(Numeric)
    period_start: Mapped[date | None] = mapped_column(Date)  # NULL for point-in-time facts
    period_end: Mapped[date] = mapped_column(Date)
    accession: Mapped[str] = mapped_column(String(20))
    form: Mapped[str] = mapped_column(String(16))
    filed_at: Mapped[date] = mapped_column(Date)
    fiscal_year: Mapped[int | None]
    fiscal_period: Mapped[str | None] = mapped_column(String(4))
    frame: Mapped[str | None] = mapped_column(String(16))


class SyncRun(Base):
    __tablename__ = "sync_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"))
    status: Mapped[SyncStatus] = mapped_column(_enum(SyncStatus, "sync_status"))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stats: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")
    error: Mapped[str | None] = mapped_column(Text)


class Metric(enum.StrEnum):
    """The metric dictionary of docs/design.md §4.3, plus `other` for guidance on
    anything else (segment revenue, capex, ...), which is stored but not scored."""

    REVENUE = "revenue"
    GROSS_MARGIN = "gross_margin"
    OPERATING_EXPENSES = "operating_expenses"
    OPERATING_MARGIN = "operating_margin"
    EPS_DILUTED = "eps_diluted"
    TAX_RATE = "tax_rate"
    OTHER = "other"


class Basis(enum.StrEnum):
    GAAP = "gaap"
    NON_GAAP = "non_gaap"
    UNSPECIFIED = "unspecified"


class Unit(enum.StrEnum):
    USD = "usd"
    PERCENT = "percent"
    USD_PER_SHARE = "usd_per_share"


class PromiseKind(enum.StrEnum):
    RANGE = "range"  # low..high
    POINT = "point"  # low == high; `approximate` widens it at resolution
    BOUND = "bound"  # at least low, or at most high
    RELATIVE = "relative"  # change vs. a reference period, resolved in M3
    QUALITATIVE = "qualitative"  # stored and shown, never scored


class PromiseStatus(enum.StrEnum):
    PENDING = "pending"
    SUPERSEDED = "superseded"
    RESOLVED = "resolved"
    UNRESOLVABLE = "unresolvable"


class ReviewStatus(enum.StrEnum):
    AUTO_ACCEPTED = "auto_accepted"
    NEEDS_REVIEW = "needs_review"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


class Promise(Base):
    """One forward-looking statement about one metric for one future period."""

    __tablename__ = "promises"
    __table_args__ = (
        Index("ix_promises_target", "company_id", "metric", "basis", "target_period_id"),
        CheckConstraint("low IS NULL OR high IS NULL OR low <= high", name="promise_low_le_high"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"))
    filing_id: Mapped[int] = mapped_column(ForeignKey("filings.id", ondelete="CASCADE"))
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"))
    issued_at: Mapped[date] = mapped_column(Date)
    issuing_period_id: Mapped[int | None] = mapped_column(
        ForeignKey("fiscal_periods.id", ondelete="SET NULL")
    )
    # As stated in the document; target_period_id is NULL if our calendar lacks it.
    target_fiscal_year: Mapped[int]
    target_fiscal_quarter: Mapped[int | None]
    target_period_id: Mapped[int | None] = mapped_column(
        ForeignKey("fiscal_periods.id", ondelete="SET NULL")
    )

    metric: Mapped[Metric] = mapped_column(_enum(Metric, "metric"))
    metric_name: Mapped[str] = mapped_column(Text)  # as the document names it
    basis: Mapped[Basis] = mapped_column(_enum(Basis, "basis"))
    kind: Mapped[PromiseKind] = mapped_column(_enum(PromiseKind, "promise_kind"))
    unit: Mapped[Unit | None] = mapped_column(_enum(Unit, "unit"))
    # Absolute values: USD, percent (52.5 means 52.5%), or USD per share.
    low: Mapped[Decimal | None] = mapped_column(Numeric)
    high: Mapped[Decimal | None] = mapped_column(Numeric)
    approximate: Mapped[bool] = mapped_column(server_default=false())
    # For RELATIVE promises: change in percent vs. the reference period's actual.
    relative_reference: Mapped[str | None] = mapped_column(String(24))
    change_low: Mapped[Decimal | None] = mapped_column(Numeric)
    change_high: Mapped[Decimal | None] = mapped_column(Numeric)
    # Set when a documented convention was applied ("mid single digits" -> 4-6%).
    interpretation: Mapped[str | None] = mapped_column(Text)

    quote: Mapped[str] = mapped_column(Text)
    span_start: Mapped[int]
    span_end: Mapped[int]
    confidence: Mapped[float]

    status: Mapped[PromiseStatus] = mapped_column(
        _enum(PromiseStatus, "promise_status"), server_default=PromiseStatus.PENDING.value
    )
    review_status: Mapped[ReviewStatus] = mapped_column(_enum(ReviewStatus, "review_status"))
    review_reasons: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default="{}")
    superseded_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("promises.id", ondelete="SET NULL")
    )

    extractor_version: Mapped[str] = mapped_column(String(32))
    llm_call_id: Mapped[int | None] = mapped_column(ForeignKey("llm_calls.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    target_period: Mapped[FiscalPeriod | None] = relationship(foreign_keys=[target_period_id])
    resolution: Mapped["Resolution | None"] = relationship(viewonly=True)


class LlmCallStatus(enum.StrEnum):
    SUCCEEDED = "succeeded"
    REFUSED = "refused"
    TRUNCATED = "truncated"
    INVALID_OUTPUT = "invalid_output"
    FAILED = "failed"


class LlmCall(Base):
    """Cost ledger: one row per model request, used for spend tracking and the budget cap."""

    __tablename__ = "llm_calls"
    __table_args__ = (Index("ix_llm_calls_created", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    task: Mapped[str] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(String(64))
    batch_id: Mapped[int | None] = mapped_column(ForeignKey("llm_batches.id", ondelete="SET NULL"))
    subject: Mapped[str | None] = mapped_column(Text)  # e.g. "document:123"
    status: Mapped[LlmCallStatus] = mapped_column(_enum(LlmCallStatus, "llm_call_status"))
    input_tokens: Mapped[int] = mapped_column(server_default="0")
    output_tokens: Mapped[int] = mapped_column(server_default="0")
    cache_read_tokens: Mapped[int] = mapped_column(server_default="0")
    cache_write_tokens: Mapped[int] = mapped_column(server_default="0")
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), server_default="0")
    latency_ms: Mapped[int | None]
    request_id: Mapped[str | None] = mapped_column(String(64))
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LlmBatchStatus(enum.StrEnum):
    SUBMITTED = "submitted"
    COLLECTED = "collected"
    FAILED = "failed"


class LlmBatch(Base):
    __tablename__ = "llm_batches"

    id: Mapped[int] = mapped_column(primary_key=True)
    anthropic_batch_id: Mapped[str] = mapped_column(String(64), unique=True)
    task: Mapped[str] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(String(64))
    request_count: Mapped[int]
    status: Mapped[LlmBatchStatus] = mapped_column(_enum(LlmBatchStatus, "llm_batch_status"))
    estimated_cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    collected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ActualSource(enum.StrEnum):
    XBRL = "xbrl"  # read from one filing's XBRL facts (or a ratio of two)
    XBRL_DERIVED = "xbrl_derived"  # Q4 = fiscal year minus nine months year-to-date
    PRESS_RELEASE = "press_release"  # extracted from the earnings release (incl. non-GAAP)


class Actual(Base):
    """A reported value of a metric for a fiscal period, from one source."""

    __tablename__ = "actuals"
    __table_args__ = (UniqueConstraint("period_id", "metric", "basis", "source"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"))
    period_id: Mapped[int] = mapped_column(ForeignKey("fiscal_periods.id", ondelete="CASCADE"))
    metric: Mapped[Metric] = mapped_column(_enum(Metric, "metric"))
    basis: Mapped[Basis] = mapped_column(_enum(Basis, "basis"))
    source: Mapped[ActualSource] = mapped_column(_enum(ActualSource, "actual_source"))
    value: Mapped[Decimal] = mapped_column(Numeric)
    unit: Mapped[Unit] = mapped_column(_enum(Unit, "unit"))
    # How the value was obtained, e.g. "GrossProfit / Revenues", and from where.
    detail: Mapped[str] = mapped_column(Text)
    accession: Mapped[str | None] = mapped_column(String(20))
    document_id: Mapped[int | None] = mapped_column(ForeignKey("documents.id", ondelete="SET NULL"))
    quote: Mapped[str | None] = mapped_column(Text)
    filed_at: Mapped[date] = mapped_column(Date)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    period: Mapped[FiscalPeriod] = relationship()


class Outcome(enum.StrEnum):
    BELOW = "below"
    LOW_END = "low_end"  # in the bottom third of a range
    MID = "mid"
    HIGH_END = "high_end"
    ABOVE = "above"
    MET = "met"  # a point or bound, within tolerance


class ResolutionStatus(enum.StrEnum):
    PROVISIONAL = "provisional"  # from the press release only
    CONFIRMED = "confirmed"  # from XBRL in a 10-Q/10-K


class Resolution(Base):
    """A promise compared with what the company later reported (docs/design.md §5.3)."""

    __tablename__ = "resolutions"

    id: Mapped[int] = mapped_column(primary_key=True)
    promise_id: Mapped[int] = mapped_column(
        ForeignKey("promises.id", ondelete="CASCADE"), unique=True
    )
    actual_id: Mapped[int] = mapped_column(ForeignKey("actuals.id", ondelete="CASCADE"))
    actual_value: Mapped[Decimal] = mapped_column(Numeric)
    # The range the actual was compared with: the promise's own range, a point
    # widened by its tolerance, or a relative promise turned into absolute values.
    expected_low: Mapped[Decimal | None] = mapped_column(Numeric)
    expected_high: Mapped[Decimal | None] = mapped_column(Numeric)
    outcome: Mapped[Outcome] = mapped_column(_enum(Outcome, "outcome"))
    # Hit: met or beaten in the metric's favorable direction. Miss otherwise.
    hit: Mapped[bool]
    # Signed so that positive is better than guided, relative to the midpoint.
    surprise_pct: Mapped[Decimal | None] = mapped_column(Numeric)
    surprise_pp: Mapped[Decimal | None] = mapped_column(Numeric)  # percent metrics only
    status: Mapped[ResolutionStatus] = mapped_column(_enum(ResolutionStatus, "resolution_status"))
    # The press release and XBRL disagree on this GAAP value.
    discrepancy: Mapped[str | None] = mapped_column(Text)
    resolved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    actual: Mapped[Actual] = relationship()
