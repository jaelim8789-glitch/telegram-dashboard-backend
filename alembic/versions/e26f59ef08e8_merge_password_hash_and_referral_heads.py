"""merge password_hash and referral heads

Revision ID: e26f59ef08e8
Revises: c9f1e3a5b7d2, f6a7b8c9d0e1
Create Date: 2026-07-22 11:04:45.140861

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e26f59ef08e8'
down_revision: Union[str, None] = ('c9f1e3a5b7d2', 'f6a7b8c9d0e1')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
