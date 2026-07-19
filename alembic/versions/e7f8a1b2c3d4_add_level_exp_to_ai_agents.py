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
    # True no-op. 2b68d1568159 (create_ai_agents_chats_messages_tables) is what
    # actually creates ai_agents, already including level/exp — but that
    # migration sits on a parallel branch merged in later by
    # drop_reply_macro_schedule, so ai_agents does not necessarily exist yet
    # when this revision runs (ALTER TABLE here raised UndefinedTableError in
    # production). This revision only still exists because that merge
    # migration references its id as one of several heads it unifies; it does
    # not need to do anything itself.
    pass


def downgrade() -> None:
    pass
