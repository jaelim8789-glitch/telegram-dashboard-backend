"""add tenant_id to accounts

Revision ID: a1b2c3d4e5f6
Revises: f8a5d3b2c1e0
Create Date: 2026-07-09 16:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, None] = "f8a5d3b2c1e0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add tenant_id to accounts table (nullable, SET NULL on delete)
    op.add_column(
        "accounts",
        sa.Column("tenant_id", sa.String(length=36), nullable=True),
    )
    op.create_index(op.f("ix_accounts_tenant_id"), "accounts", ["tenant_id"], unique=False)
    op.create_foreign_key(
        "fk_accounts_tenant_id",
        "accounts", "tenants",
        ["tenant_id"], ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_accounts_tenant_id", "accounts", type_="foreignkey")
    op.drop_index(op.f("ix_accounts_tenant_id"), table_name="accounts")
    op.drop_column("accounts", "tenant_id")