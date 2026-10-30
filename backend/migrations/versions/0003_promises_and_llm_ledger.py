"""promises and llm ledger

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-30 15:29:48.294566

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "llm_batches",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("anthropic_batch_id", sa.String(length=64), nullable=False),
        sa.Column("task", sa.String(length=64), nullable=False),
        sa.Column("model", sa.String(length=64), nullable=False),
        sa.Column("request_count", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.Enum("submitted", "collected", "failed", name="llm_batch_status"),
            nullable=False,
        ),
        sa.Column("estimated_cost_usd", sa.Numeric(precision=12, scale=6), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("collected_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("anthropic_batch_id"),
    )
    op.create_table(
        "llm_calls",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("task", sa.String(length=64), nullable=False),
        sa.Column("model", sa.String(length=64), nullable=False),
        sa.Column("batch_id", sa.Integer(), nullable=True),
        sa.Column("subject", sa.Text(), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "succeeded",
                "refused",
                "truncated",
                "invalid_output",
                "failed",
                name="llm_call_status",
            ),
            nullable=False,
        ),
        sa.Column("input_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("output_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("cache_read_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("cache_write_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "cost_usd", sa.Numeric(precision=12, scale=6), server_default="0", nullable=False
        ),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("request_id", sa.String(length=64), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["batch_id"], ["llm_batches.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_llm_calls_created", "llm_calls", ["created_at"], unique=False)
    op.create_table(
        "promises",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("filing_id", sa.Integer(), nullable=False),
        sa.Column("document_id", sa.Integer(), nullable=False),
        sa.Column("issued_at", sa.Date(), nullable=False),
        sa.Column("issuing_period_id", sa.Integer(), nullable=True),
        sa.Column("target_fiscal_year", sa.Integer(), nullable=False),
        sa.Column("target_fiscal_quarter", sa.Integer(), nullable=True),
        sa.Column("target_period_id", sa.Integer(), nullable=True),
        sa.Column(
            "metric",
            sa.Enum(
                "revenue",
                "gross_margin",
                "operating_expenses",
                "operating_margin",
                "eps_diluted",
                "tax_rate",
                "other",
                name="metric",
            ),
            nullable=False,
        ),
        sa.Column("metric_name", sa.Text(), nullable=False),
        sa.Column(
            "basis", sa.Enum("gaap", "non_gaap", "unspecified", name="basis"), nullable=False
        ),
        sa.Column(
            "kind",
            sa.Enum("range", "point", "bound", "relative", "qualitative", name="promise_kind"),
            nullable=False,
        ),
        sa.Column("unit", sa.Enum("usd", "percent", "usd_per_share", name="unit"), nullable=True),
        sa.Column("low", sa.Numeric(), nullable=True),
        sa.Column("high", sa.Numeric(), nullable=True),
        sa.Column("approximate", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("relative_reference", sa.String(length=24), nullable=True),
        sa.Column("change_low", sa.Numeric(), nullable=True),
        sa.Column("change_high", sa.Numeric(), nullable=True),
        sa.Column("interpretation", sa.Text(), nullable=True),
        sa.Column("quote", sa.Text(), nullable=False),
        sa.Column("span_start", sa.Integer(), nullable=False),
        sa.Column("span_end", sa.Integer(), nullable=False),
        sa.Column("confidence", sa.Double(), nullable=False),
        sa.Column(
            "status",
            sa.Enum("pending", "superseded", "resolved", "unresolvable", name="promise_status"),
            server_default="pending",
            nullable=False,
        ),
        sa.Column(
            "review_status",
            sa.Enum("auto_accepted", "needs_review", "accepted", "rejected", name="review_status"),
            nullable=False,
        ),
        sa.Column(
            "review_reasons", postgresql.ARRAY(sa.Text()), server_default="{}", nullable=False
        ),
        sa.Column("superseded_by_id", sa.Integer(), nullable=True),
        sa.Column("extractor_version", sa.String(length=32), nullable=False),
        sa.Column("llm_call_id", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "low IS NULL OR high IS NULL OR low <= high", name="promise_low_le_high"
        ),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["filing_id"], ["filings.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["issuing_period_id"], ["fiscal_periods.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["llm_call_id"], ["llm_calls.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["superseded_by_id"], ["promises.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["target_period_id"], ["fiscal_periods.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_promises_target",
        "promises",
        ["company_id", "metric", "basis", "target_period_id"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_promises_target", table_name="promises")
    op.drop_table("promises")
    op.drop_index("ix_llm_calls_created", table_name="llm_calls")
    op.drop_table("llm_calls")
    op.drop_table("llm_batches")
    for name in (
        "metric",
        "basis",
        "promise_kind",
        "unit",
        "promise_status",
        "review_status",
        "llm_call_status",
        "llm_batch_status",
    ):
        sa.Enum(name=name).drop(op.get_bind())
