"""merge distribution and style-profiles heads

Revision ID: 2ccdcc70c303
Revises: a3f9c1d7e6b2, add_tenant_id_to_style_profiles
Create Date: 2026-07-20 13:56:46.739294

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '2ccdcc70c303'
down_revision: Union[str, None] = ('a3f9c1d7e6b2', 'add_tenant_id_to_style_profiles')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
