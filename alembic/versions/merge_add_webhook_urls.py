"""Merge heads + add webhook_urls to tenants

Merges the two current heads (a2b4c6d8e0f1, merge_heads_20260713) and
adds the webhook_urls column (Text, default="[]") used by the new
webhook notification service.

Revision ID: merge_add_webhook_urls
Revises: a2b4c6d8e0f1, merge_heads_20260713
Create Date: 2026-07-17 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "merge_add_webhook_urls"
down_revision: Union[str, Sequence[str], None] = ("a2b4c6d8e0f1", "merge_heads_20260713")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add webhook_urls column (JSON array of URL strings, stored as Text)
    op.add_column(
        "tenants",
        sa.Column("webhook_urls", sa.Text(), nullable=False, server_default="[]"),
    )


def downgrade() -> None:
    op.drop_column("tenants", "webhook_urls")
