"""Idempotent migration utilities.

Use these helpers in alembic migrations to make them safe to re-run.
"""

from alembic import op
import sqlalchemy as sa


def create_table_if_not_exists(table_name: str, *args, **kwargs):
    """Create a table only if it doesn't already exist."""
    bind = op.get_bind()
    if not bind.dialect.has_table(bind, table_name):
        op.create_table(table_name, *args, **kwargs)


def drop_table_if_exists(table_name: str):
    """Drop a table only if it exists."""
    bind = op.get_bind()
    if bind.dialect.has_table(bind, table_name):
        op.drop_table(table_name)


def add_column_if_not_exists(table_name: str, column: sa.Column):
    """Add a column only if it doesn't already exist."""
    bind = op.get_bind()
    insp = sa.inspect(bind)
    columns = [c["name"] for c in insp.get_columns(table_name)]
    if column.name not in columns:
        op.add_column(table_name, column)
