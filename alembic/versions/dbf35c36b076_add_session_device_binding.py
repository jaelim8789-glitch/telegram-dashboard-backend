"""add session device binding

Revision ID: dbf35c36b076
Revises: 8bbf60a9fe07
Create Date: 2026-08-04 00:26:43.212883

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from migration_helpers import add_column_if_not_exists, drop_column_if_exists


# revision identifiers, used by Alembic.
revision: str = 'dbf35c36b076'
down_revision: Union[str, None] = '8bbf60a9fe07'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Bind each session to the browser/device that created it: stored as
    # SHA-256 hashes (never raw UA/IP — they're semi-identifying), plus a
    # persistent requires_reauth flag that get_session_by_token flips when a
    # presented token comes from a mismatched device/IP (soft enforcement:
    # the session is still returned, the flag just signals re-auth).
    add_column_if_not_exists(
        "user_sessions",
        sa.Column("user_agent_hash", sa.String(length=64), nullable=True),
    )
    add_column_if_not_exists(
        "user_sessions",
        sa.Column("client_ip_hash", sa.String(length=64), nullable=True),
    )
    add_column_if_not_exists(
        "user_sessions",
        sa.Column("requires_reauth", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    drop_column_if_exists("user_sessions", "requires_reauth")
    drop_column_if_exists("user_sessions", "client_ip_hash")
    drop_column_if_exists("user_sessions", "user_agent_hash")
