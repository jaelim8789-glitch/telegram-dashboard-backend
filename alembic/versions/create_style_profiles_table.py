"""create style_profiles table for AI tone learning

Revision ID: create_style_profiles
Revises: add_campaign_fields_broadcasts
Create Date: 2026-07-20 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "create_style_profiles"
down_revision: Union[str, None] = "add_campaign_fields_broadcasts"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "style_profiles",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("source_type", sa.String(20), nullable=False),
        sa.Column("source_text", sa.Text, nullable=False),
        sa.Column("tone_analysis", sa.JSON, nullable=False, server_default="{}"),
        sa.Column("style_prompt", sa.Text, nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime, server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime, server_default=sa.text("now()"), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("style_profiles")
