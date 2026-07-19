"""add level and exp columns to ai_agents

Revision ID: e7f8a1b2c3d4
Revises: ca93a1138163
Create Date: 2026-07-19 19:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "e7f8a1b2c3d4"
down_revision: Union[str, None] = "ca93a1138163"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Idempotent: 2b68d1568159 (create_ai_agents_chats_messages_tables) already
    # creates ai_agents with level/exp columns on any DB that ran migrations in
    # order. This revision only still exists because a later merge migration
    # (drop_reply_macro_schedule) references it as one of several heads it
    # unifies — recreated here as a no-op-if-already-present safeguard rather
    # than removing it, so `alembic upgrade head` doesn't KeyError on the
    # missing revision.
    op.execute("ALTER TABLE ai_agents ADD COLUMN IF NOT EXISTS level INTEGER NOT NULL DEFAULT 1")
    op.execute("ALTER TABLE ai_agents ADD COLUMN IF NOT EXISTS exp INTEGER NOT NULL DEFAULT 0")


def downgrade() -> None:
    op.execute("ALTER TABLE ai_agents DROP COLUMN IF EXISTS exp")
    op.execute("ALTER TABLE ai_agents DROP COLUMN IF EXISTS level")
