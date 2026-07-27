"""Add is_distributor field to tenants table

Revision ID: f1e2d3c4b5a6
Revises: f8a5d3b2c1e0
Create Date: 2026-07-21 10:31:33.123456

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f1e2d3c4b5a6'
down_revision: Union[str, None] = 'f8a5d3b2c1e0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    #  is_distributor  
    op.add_column('tenants', sa.Column('is_distributor', sa.Boolean(), nullable=True))
    
    #     False 
    op.execute("UPDATE tenants SET is_distributor = FALSE WHERE is_distributor IS NULL")
    
    #  NOT NULL 
    op.alter_column('tenants', 'is_distributor', nullable=False)


def downgrade() -> None:
    #  is_distributor  
    op.drop_column('tenants', 'is_distributor')