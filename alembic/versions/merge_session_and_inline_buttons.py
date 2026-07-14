"""merge add_inline_buttons and add_user_sessions heads"""

revision: str = "merge_session_and_inline_buttons"
down_revision: tuple[str, str] = ("add_inline_buttons", "add_user_sessions")
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
