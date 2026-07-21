"""merge referral_code_uses and telegram_id_bigint heads

Revision ID: 46332ba7d956
Revises: add_referral_code_uses, e1a2b3c4d5e6
Create Date: 2026-07-21 11:54:09.437524

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '46332ba7d956'
down_revision: Union[str, None] = ('add_referral_code_uses', 'e1a2b3c4d5e6')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
