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
