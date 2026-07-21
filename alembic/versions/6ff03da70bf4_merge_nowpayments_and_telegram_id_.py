"""merge nowpayments and telegram_id_bigint/referral heads

The z9a0b1c2d3e4 (add_nowpayments_transactions_table) migration file was
removed as a duplicate/orphan during an earlier head-cleanup, but its
revision id was already applied on production (recorded in
alembic_version there), so this merge point stays as history — it just
no longer needs to reference the deleted file's revision as a parent.

Revision ID: 6ff03da70bf4
Revises: 46332ba7d956
Create Date: 2026-07-21 14:02:51.932562

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '6ff03da70bf4'
down_revision: Union[str, None] = '46332ba7d956'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
