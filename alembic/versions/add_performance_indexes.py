"""Add performance indexes for common query patterns

Revision ID: 004_add_performance_indexes
Revises: 003_create_kb_tables
Create Date: 2026-07-30 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op

revision: str = "004_add_performance_indexes"
down_revision: Union[str, None] = "003_create_kb_tables"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Broadcasts: most queries filter by status and created_at
    op.execute("CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_broadcasts_status ON broadcasts(status)")
    op.execute("CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_broadcasts_account_id ON broadcasts(account_id)")
    op.execute("CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_broadcasts_created_at ON broadcasts(created_at DESC)")

    # KB chunks: embedding search and document lookups
    op.execute("CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_kb_chunks_doc_id ON kb_chunks(document_id)")
    op.execute("CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_kb_chunks_created ON kb_chunks(created_at DESC)")

    # Search logs: admin stats queries by date
    op.execute("CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_kb_search_logs_created ON kb_search_logs(created_at DESC)")

    # Account health: per-account lookups
    op.execute("CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_account_health_account ON account_health(account_id)")

    # Message logs: filtering by status and date
    op.execute("CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_message_logs_status ON message_logs(status)")
    op.execute("CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_message_logs_created ON message_logs(created_at DESC)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_broadcasts_status")
    op.execute("DROP INDEX IF EXISTS idx_broadcasts_account_id")
    op.execute("DROP INDEX IF EXISTS idx_broadcasts_created_at")
    op.execute("DROP INDEX IF EXISTS idx_kb_chunks_doc_id")
    op.execute("DROP INDEX IF EXISTS idx_kb_chunks_created")
    op.execute("DROP INDEX IF EXISTS idx_kb_search_logs_created")
    op.execute("DROP INDEX IF EXISTS idx_account_health_account")
    op.execute("DROP INDEX IF EXISTS idx_message_logs_status")
    op.execute("DROP INDEX IF EXISTS idx_message_logs_created")
