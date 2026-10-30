"""actuals and resolutions

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-30 17:16:23.262756

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "actuals",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("period_id", sa.Integer(), nullable=False),
        sa.Column(
            "metric",
            postgresql.ENUM(
                "revenue",
                "gross_margin",
                "operating_expenses",
                "operating_margin",
                "eps_diluted",
                "tax_rate",
                "other",
                name="metric",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column(
            "basis",
            postgresql.ENUM("gaap", "non_gaap", "unspecified", name="basis", create_type=False),
            nullable=False,
        ),
        sa.Column(
            "source",
            sa.Enum("xbrl", "xbrl_derived", "press_release", name="actual_source"),
            nullable=False,
        ),
        sa.Column("value", sa.Numeric(), nullable=False),
        sa.Column(
            "unit",
            postgresql.ENUM("usd", "percent", "usd_per_share", name="unit", create_type=False),
            nullable=False,
        ),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column("accession", sa.String(length=20), nullable=True),
        sa.Column("document_id", sa.Integer(), nullable=True),
        sa.Column("quote", sa.Text(), nullable=True),
        sa.Column("filed_at", sa.Date(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["period_id"], ["fiscal_periods.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("period_id", "metric", "basis", "source"),
    )
    op.create_table(
        "resolutions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("promise_id", sa.Integer(), nullable=False),
        sa.Column("actual_id", sa.Integer(), nullable=False),
        sa.Column("actual_value", sa.Numeric(), nullable=False),
        sa.Column("expected_low", sa.Numeric(), nullable=True),
        sa.Column("expected_high", sa.Numeric(), nullable=True),
        sa.Column(
            "outcome",
            sa.Enum("below", "low_end", "mid", "high_end", "above", "met", name="outcome"),
            nullable=False,
        ),
        sa.Column("hit", sa.Boolean(), nullable=False),
        sa.Column("surprise_pct", sa.Numeric(), nullable=True),
        sa.Column("surprise_pp", sa.Numeric(), nullable=True),
        sa.Column(
            "status", sa.Enum("provisional", "confirmed", name="resolution_status"), nullable=False
        ),
        sa.Column("discrepancy", sa.Text(), nullable=True),
        sa.Column(
            "resolved_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["actual_id"], ["actuals.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["promise_id"], ["promises.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("promise_id"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("resolutions")
    op.drop_table("actuals")
    for name in ("actual_source", "outcome", "resolution_status"):
        sa.Enum(name=name).drop(op.get_bind())
