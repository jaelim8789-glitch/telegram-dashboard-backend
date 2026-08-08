"""create blacklisted_entities table for IP/device blacklist

Revision ID: create_blacklisted_entities
Revises: add_emotion_analysis
Create Date: 2026-08-08

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "create_blacklisted_entities"
down_revision = "add_emotion_analysis"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "blacklisted_entities",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, default=sa.text("gen_random_uuid()")),
        sa.Column("entity_value", sa.String, nullable=False),
        sa.Column("entity_type", sa.String, nullable=False),
        sa.Column("reason", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.text("now()")),
        sa.Column("expires_at", sa.DateTime, nullable=True),
        sa.Column("blocked_by", sa.String, nullable=False, server_default="MANUAL"),
    )
    op.create_index("ix_blacklisted_entities_entity_value", "blacklisted_entities", ["entity_value"])
    op.create_index("ix_blacklisted_entities_entity_type", "blacklisted_entities", ["entity_type"])


def downgrade() -> None:
    op.drop_index("ix_blacklisted_entities_entity_type", table_name="blacklisted_entities")
    op.drop_index("ix_blacklisted_entities_entity_value", table_name="blacklisted_entities")
    op.drop_table("blacklisted_entities")