"""add tenant_id column to style_profiles for multi-tenant isolation

Revision ID: add_tenant_id_to_style_profiles
Revises: create_style_profiles
Create Date: 2026-07-20 12:20:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "add_tenant_id_to_style_profiles"
down_revision: Union[str, None] = "create_style_profiles"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "style_profiles",
        sa.Column("tenant_id", sa.String(36), nullable=True, index=True),
    )


def downgrade() -> None:
    op.drop_column("style_profiles", "tenant_id")
