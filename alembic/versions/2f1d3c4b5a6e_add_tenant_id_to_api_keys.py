"""add tenant_id to api_keys

Revision ID: 2f1d3c4b5a6e
Revises: e6f7a8b9c0d1
Create Date: 2026-07-12 09:40:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "2f1d3c4b5a6e"
down_revision: Union[str, None] = "e6f7a8b9c0d1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("api_keys", sa.Column("tenant_id", sa.String(length=36), nullable=True))
    op.create_index(op.f("ix_api_keys_tenant_id"), "api_keys", ["tenant_id"], unique=False)
    op.create_foreign_key(
        "fk_api_keys_tenant_id",
        "api_keys",
        "tenants",
        ["tenant_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_api_keys_tenant_id", "api_keys", type_="foreignkey")
    op.drop_index(op.f("ix_api_keys_tenant_id"), table_name="api_keys")
    op.drop_column("api_keys", "tenant_id")
