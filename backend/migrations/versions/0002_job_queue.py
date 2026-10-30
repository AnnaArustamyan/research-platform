"""job queue schema (procrastinate)

Revision ID: 0002
Revises: 0001

Creates procrastinate's tables and functions. When upgrading procrastinate,
add a new revision that runs the SQL files it ships in
procrastinate/sql/migrations for the versions in between.
"""

from collections.abc import Sequence

from alembic import op
from procrastinate.schema import SchemaManager

revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(SchemaManager.get_schema())


def downgrade() -> None:
    raise NotImplementedError("procrastinate's schema has no downgrade; drop the database instead")
