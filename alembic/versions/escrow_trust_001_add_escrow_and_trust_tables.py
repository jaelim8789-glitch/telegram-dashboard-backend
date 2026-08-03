"""add escrow and trust tables

Revision ID: escrow_trust_001
Revises: merge_folders_and_reply_macro_heads
Create Date: 2026-08-03
"""

from alembic import op
import sqlalchemy as sa
from migration_helpers import create_table_if_not_exists


# revision identifiers
revision = "escrow_trust_001"
down_revision = "f7a8b9c0d1e2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Escrow
    create_table_if_not_exists(
        "escrows",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), index=True),
        sa.Column("account_id", sa.String(36), index=True),
        sa.Column("chat_id", sa.String(36), index=True),
        sa.Column("buyer_id", sa.String(36), index=True),
        sa.Column("seller_id", sa.String(36), index=True),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("amount", sa.Float, nullable=False),
        sa.Column("currency", sa.String(10), server_default="KRW"),
        sa.Column("status", sa.String(20), server_default="pending", index=True),
        sa.Column("payment_id", sa.String(100), nullable=True),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("disputed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dispute_reason", sa.Text, nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolution", sa.Text, nullable=True),
        sa.Column("metadata_json", sa.JSON, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    # Escrow Milestones
    create_table_if_not_exists(
        "escrow_milestones",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("escrow_id", sa.String(36), sa.ForeignKey("escrows.id", ondelete="CASCADE"), index=True),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("amount", sa.Float, nullable=False),
        sa.Column("order_index", sa.Integer, server_default="0"),
        sa.Column("status", sa.String(20), server_default="pending"),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    # Escrow Messages
    create_table_if_not_exists(
        "escrow_messages",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("escrow_id", sa.String(36), sa.ForeignKey("escrows.id", ondelete="CASCADE"), index=True),
        sa.Column("sender_id", sa.String(36), nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("message_type", sa.String(20), server_default="text"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    # Trust Profiles
    create_table_if_not_exists(
        "trust_profiles",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), index=True),
        sa.Column("telegram_user_id", sa.String(36), index=True),
        sa.Column("display_name", sa.String(200), nullable=True),
        sa.Column("trust_score", sa.Float, server_default="0"),
        sa.Column("total_transactions", sa.Integer, server_default="0"),
        sa.Column("successful_transactions", sa.Integer, server_default="0"),
        sa.Column("disputed_transactions", sa.Integer, server_default="0"),
        sa.Column("total_amount", sa.Float, server_default="0"),
        sa.Column("avg_rating", sa.Float, server_default="0"),
        sa.Column("badges", sa.JSON, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    # Transaction Reviews
    create_table_if_not_exists(
        "transaction_reviews",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("escrow_id", sa.String(36), sa.ForeignKey("escrows.id", ondelete="CASCADE"), index=True),
        sa.Column("tenant_id", sa.String(36), index=True),
        sa.Column("reviewer_id", sa.String(36), index=True),
        sa.Column("target_id", sa.String(36), index=True),
        sa.Column("rating", sa.Integer, nullable=False),
        sa.Column("comment", sa.Text, nullable=True),
        sa.Column("tags", sa.JSON, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("transaction_reviews")
    op.drop_table("trust_profiles")
    op.drop_table("escrow_messages")
    op.drop_table("escrow_milestones")
    op.drop_table("escrows")
