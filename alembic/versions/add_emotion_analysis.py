"""add emotion analysis columns to ai_chat_messages_v2

Revision ID: add_emotion_analysis
Revises: create_ai_memories
Create Date: 2026-08-08

"""
import sqlalchemy as sa

from migration_helpers import add_column_if_not_exists, create_index_if_not_exists
revision = "add_emotion_analysis"
down_revision = "create_ai_memories"
branch_labels = None
depends_on = None


def upgrade() -> None:
    add_column_if_not_exists("ai_chat_messages_v2", sa.Column("emotion_label", sa.String(length=20), nullable=True))
    add_column_if_not_exists("ai_chat_messages_v2", sa.Column("emotion_confidence", sa.Float(), nullable=True))
    add_column_if_not_exists("ai_chat_messages_v2", sa.Column("emotion_tone", sa.String(length=30), nullable=True))
    create_index_if_not_exists("ix_ai_chat_messages_v2_emotion_label", "ai_chat_messages_v2", ["emotion_label"])


def downgrade() -> None:
    drop_column_if_exists("ai_chat_messages_v2", "emotion_tone")
    drop_column_if_exists("ai_chat_messages_v2", "emotion_confidence")
    drop_column_if_exists("ai_chat_messages_v2", "emotion_label")
