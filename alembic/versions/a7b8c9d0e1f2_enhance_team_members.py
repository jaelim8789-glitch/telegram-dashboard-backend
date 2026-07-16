"""enhance team_members with team management fields

Revision ID: a7b8c9d0e1f2
Revises: merge_add_webhook_urls
Create Date: 2026-07-17 02:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "a7b8c9d0e1f2"
down_revision: Union[str, None] = "merge_add_webhook_urls"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add new columns to existing team_members table
    op.add_column("team_members", sa.Column("display_name", sa.String(100), nullable=True))
    op.add_column("team_members", sa.Column("phone", sa.String(50), nullable=True))
    op.add_column("team_members", sa.Column("invited_by", sa.String(36), nullable=True))
    op.add_column("team_members", sa.Column("invite_token", sa.String(64), nullable=True, unique=True))
    op.add_column("team_members", sa.Column("invite_expires_at", sa.DateTime(), nullable=True))
    op.add_column("team_members", sa.Column("invited_at", sa.DateTime(), nullable=True))
    op.add_column("team_members", sa.Column("joined_at", sa.DateTime(), nullable=True))
    op.add_column("team_members", sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False))

    # Update existing role values: 'operator' -> 'member', 'admin' stays, 'viewer' -> 'member'
    op.execute("UPDATE team_members SET role = 'member' WHERE role = 'operator'")
    op.execute("UPDATE team_members SET role = 'member' WHERE role = 'viewer'")


def downgrade() -> None:
    op.drop_column("team_members", "updated_at")
    op.drop_column("team_members", "joined_at")
    op.drop_column("team_members", "invited_at")
    op.drop_column("team_members", "invite_expires_at")
    op.drop_column("team_members", "invite_token")
    op.drop_column("team_members", "invited_by")
    op.drop_column("team_members", "phone")
    op.drop_column("team_members", "display_name")