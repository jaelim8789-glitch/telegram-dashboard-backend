"""add expires_at to referral_codes, referral_config, referral_audit_logs

Revision ID: f6a7b8c9d0e1
Revises: d4e5f6a7b8c9
Create Date: 2026-07-20 03:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.engine import reflection


revision: str = "f6a7b8c9d0e1"
down_revision: Union[str, None] = "d4e5f6a7b8c9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = reflection.Inspector.from_engine(bind)
    existing_tables = inspector.get_table_names()

    try:
        cols = [c["name"] for c in inspector.get_columns("referral_codes")]
        if "expires_at" not in cols:
            op.add_column("referral_codes", sa.Column("expires_at", sa.DateTime(), nullable=True))
    except Exception:
        pass

    if "referral_config" not in existing_tables:
        op.create_table(
            "referral_config",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("key", sa.String(length=50), nullable=False),
            sa.Column("value", sa.String(length=255), nullable=False),
            sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(op.f("ix_referral_config_key"), "referral_config", ["key"], unique=True)

    if "referral_audit_logs" not in existing_tables:
        op.create_table(
            "referral_audit_logs",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("action", sa.String(length=50), nullable=False),
            sa.Column("actor_id", sa.String(length=36), nullable=True),
            sa.Column("target_id", sa.String(length=36), nullable=True),
            sa.Column("details", sa.String(length=500), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(op.f("ix_referral_audit_logs_action"), "referral_audit_logs", ["action"], unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = reflection.Inspector.from_engine(bind)
    existing_tables = inspector.get_table_names()

    if "referral_audit_logs" in existing_tables:
        op.drop_index(op.f("ix_referral_audit_logs_action"), table_name="referral_audit_logs")
        op.drop_table("referral_audit_logs")
    if "referral_config" in existing_tables:
        op.drop_index(op.f("ix_referral_config_key"), table_name="referral_config")
        op.drop_table("referral_config")

    try:
        cols = [c["name"] for c in inspector.get_columns("referral_codes")]
        if "expires_at" in cols:
            op.drop_column("referral_codes", "expires_at")
    except Exception:
        pass
