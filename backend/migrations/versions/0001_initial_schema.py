"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-10-30 14:44:54.932562

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "companies",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("cik", sa.Integer(), nullable=False),
        sa.Column("ticker", sa.String(length=12), nullable=False),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("sic_description", sa.Text(), nullable=True),
        sa.Column("fiscal_year_end", sa.String(length=4), nullable=True),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("guides_quarterly", sa.Boolean(), nullable=True),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("cik"),
        sa.UniqueConstraint("ticker"),
    )
    op.create_table(
        "fiscal_periods",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("fiscal_year", sa.Integer(), nullable=False),
        sa.Column("fiscal_quarter", sa.Integer(), nullable=True),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("is_estimated", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.CheckConstraint("fiscal_quarter BETWEEN 1 AND 4", name="fiscal_quarter_range"),
        sa.CheckConstraint("start_date <= end_date", name="period_dates_ordered"),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "company_id", "fiscal_year", "fiscal_quarter", postgresql_nulls_not_distinct=True
        ),
    )
    op.create_table(
        "sync_runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column(
            "status", sa.Enum("running", "succeeded", "failed", name="sync_status"), nullable=False
        ),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "stats", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "xbrl_facts",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("taxonomy", sa.String(length=16), nullable=False),
        sa.Column("concept", sa.Text(), nullable=False),
        sa.Column("unit", sa.String(length=32), nullable=False),
        sa.Column("value", sa.Numeric(), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=True),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("accession", sa.String(length=20), nullable=False),
        sa.Column("form", sa.String(length=16), nullable=False),
        sa.Column("filed_at", sa.Date(), nullable=False),
        sa.Column("fiscal_year", sa.Integer(), nullable=True),
        sa.Column("fiscal_period", sa.String(length=4), nullable=True),
        sa.Column("frame", sa.String(length=16), nullable=True),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "company_id",
            "taxonomy",
            "concept",
            "unit",
            "period_start",
            "period_end",
            "accession",
            postgresql_nulls_not_distinct=True,
        ),
    )
    op.create_index(
        "ix_xbrl_facts_lookup", "xbrl_facts", ["company_id", "concept", "period_end"], unique=False
    )
    op.create_table(
        "filings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("accession", sa.String(length=20), nullable=False),
        sa.Column("form", sa.String(length=16), nullable=False),
        sa.Column(
            "kind",
            sa.Enum("earnings_release", "quarterly_report", "annual_report", name="filing_kind"),
            nullable=False,
        ),
        sa.Column("is_amendment", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("filed_at", sa.Date(), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("report_date", sa.Date(), nullable=True),
        sa.Column(
            "items", postgresql.ARRAY(sa.String(length=8)), server_default="{}", nullable=False
        ),
        sa.Column("primary_document", sa.Text(), nullable=True),
        sa.Column("period_id", sa.Integer(), nullable=True),
        sa.Column("documents_fetched_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["period_id"], ["fiscal_periods.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("accession"),
    )
    op.create_index("ix_filings_company_filed", "filings", ["company_id", "filed_at"], unique=False)
    op.create_table(
        "documents",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("filing_id", sa.Integer(), nullable=False),
        sa.Column("doc_type", sa.String(length=32), nullable=False),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=True),
        sa.Column("sha256", sa.String(length=64), nullable=True),
        sa.Column("storage_path", sa.Text(), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column("parser_version", sa.String(length=32), nullable=True),
        sa.Column("parsed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["filing_id"], ["filings.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("filing_id", "filename"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("documents")
    op.drop_index("ix_filings_company_filed", table_name="filings")
    op.drop_table("filings")
    op.drop_index("ix_xbrl_facts_lookup", table_name="xbrl_facts")
    op.drop_table("xbrl_facts")
    op.drop_table("sync_runs")
    op.drop_table("fiscal_periods")
    op.drop_table("companies")
    sa.Enum(name="filing_kind").drop(op.get_bind())
    sa.Enum(name="sync_status").drop(op.get_bind())
