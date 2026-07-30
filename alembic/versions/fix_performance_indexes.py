"""Fix performance indexes — remove account_health index (table may not exist)

Revision ID: 005_fix_performance_indexes
Revises: 004_add_performance_indexes
Create Date: 2026-07-30 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
from sqlalchemy import text

revision: str = "005_fix_performance_indexes"
down_revision: Union[str, None] = "004_add_performance_indexes"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE_EXISTS_SQL = "SELECT EXISTS (SELECT FROM information_schema.tables WHERE table_name = :t)"


def upgrade() -> None:
    conn = op.get_bind()
    # Only create account_health index if the table exists
    result = conn.execute(text(TABLE_EXISTS_SQL), {"t": "account_health"}).scalar()
    if result:
        with op.get_context().autocommit_block():
            op.execute("CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_account_health_account ON account_health(account_id)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_account_health_account")
