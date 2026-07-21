"""Add NOWPayments transactions table

Revision ID: z9a0b1c2d3e4
Revises: f8a5d3b2c1e0
Create Date: 2024-12-20 10:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers
revision = 'z9a0b1c2d3e4'
down_revision = 'f8a5d3b2c1e0'  # 가장 최근 마이그레이션 ID로 설정
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Create nowpayments_transactions table
    op.create_table(
        'nowpayments_transactions',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('payment_id', sa.String(), nullable=False),
        sa.Column('tenant_id', sa.String(), nullable=False),
        sa.Column('plan_id', sa.String(), nullable=False),
        sa.Column('amount', sa.Float(), nullable=False),
        sa.Column('pay_currency', sa.String(), nullable=False),
        sa.Column('paid_amount', sa.Float(), nullable=True),
        sa.Column('order_id', sa.String(), nullable=False),
        sa.Column('payment_status', sa.String(), nullable=False),
        sa.Column('note', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('payment_id'),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ),
        sa.Index('ix_nowpayments_transactions_tenant_id', 'tenant_id'),
        sa.Index('ix_nowpayments_transactions_payment_id', 'payment_id'),
        sa.Index('ix_nowpayments_transactions_order_id', 'order_id'),
        sa.Index('ix_nowpayments_transactions_payment_status', 'payment_status')
    )


def downgrade() -> None:
    # Drop nowpayments_transactions table
    op.drop_table('nowpayments_transactions')