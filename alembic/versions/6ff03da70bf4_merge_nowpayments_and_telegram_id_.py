"""merge nowpayments and telegram_id_bigint/referral heads

Revision ID: 6ff03da70bf4
Revises: 46332ba7d956, z9a0b1c2d3e4
Create Date: 2026-07-21 14:02:51.932562

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '6ff03da70bf4'
down_revision: Union[str, None] = ('46332ba7d956', 'z9a0b1c2d3e4')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
