"""merge guest_signed_up_after and session_pinned heads

Revision ID: 3dbfcaede101
Revises: add_guest_signed_up_after, add_session_pinned
Create Date: 2026-08-06 05:39:44.522124

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '3dbfcaede101'
down_revision: Union[str, None] = ('add_guest_signed_up_after', 'add_session_pinned')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
