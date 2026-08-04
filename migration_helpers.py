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
    """Add a column only if it doesn't already exist (skips missing tables)."""
    bind = op.get_bind()
    if not bind.dialect.has_table(bind, table_name):
        return
    insp = sa.inspect(bind)
    columns = [c["name"] for c in insp.get_columns(table_name)]
    if column.name not in columns:
        op.add_column(table_name, column)


def drop_column_if_exists(table_name: str, column_name: str):
    """Drop a column only if it exists (skips missing tables)."""
    bind = op.get_bind()
    if not bind.dialect.has_table(bind, table_name):
        return
    insp = sa.inspect(bind)
    columns = [c["name"] for c in insp.get_columns(table_name)]
    if column_name in columns:
        op.drop_column(table_name, column_name)


def create_index_if_not_exists(index_name: str, table_name: str, columns, **kwargs):
    """Create an index only if it doesn't already exist (skips missing tables)."""
    bind = op.get_bind()
    if not bind.dialect.has_table(bind, table_name):
        return
    insp = sa.inspect(bind)
    existing = [ix["name"] for ix in insp.get_indexes(table_name)]
    if index_name not in existing:
        op.create_index(index_name, table_name, columns, **kwargs)


def create_foreign_key_if_not_exists(constraint_name: str, table_name: str, *args, **kwargs):
    """Create a foreign key constraint only if it doesn't already exist (skips missing tables)."""
    bind = op.get_bind()
    if not bind.dialect.has_table(bind, table_name):
        return
    insp = sa.inspect(bind)
    existing = [fk["name"] for fk in insp.get_foreign_keys(table_name)]
    if constraint_name not in existing:
        op.create_foreign_key(constraint_name, table_name, *args, **kwargs)


def create_unique_constraint_if_not_exists(constraint_name: str, table_name: str, columns, **kwargs):
    """Create a unique constraint only if it doesn't already exist (skips missing tables)."""
    bind = op.get_bind()
    if not bind.dialect.has_table(bind, table_name):
        return
    insp = sa.inspect(bind)
    existing = [uc["name"] for uc in insp.get_unique_constraints(table_name)]
    if constraint_name not in existing:
        op.create_unique_constraint(constraint_name, table_name, columns, **kwargs)
