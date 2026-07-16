"""merge folders table and reply_macro reply_to_message_id heads"""

revision: str = "merge_folders_and_reply_macro_heads"
down_revision: tuple[str, str] = ("b3c7f1a9d2e4", "b5d7e9f1a2c3")
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
