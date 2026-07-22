"""
Validate that Alembic downgrade operations actually work.

This test:
  1. Applies all pending migrations (upgrade head)
  2. Downgrades by one revision
  3. Upgrades back to head
  4. Verifies the database ends up in a consistent state

Run:
    pytest tests/test_alembic_downgrade.py -v --runslow

Note: marked with @pytest.mark.slow because it modifies the actual database.
      Use -k "alembic" to run just these tests without the slow marker.
"""

from __future__ import annotations

import os

import pytest

# Mark all tests in this module as slow
pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(
        os.environ.get("DATABASE_URL", "").startswith("sqlite"),
        reason="Alembic downgrade test requires a real database (Postgres)",
    ),
]


@pytest.fixture(scope="module")
def alembic_cfg():
    """Return Alembic config pointing at the test database."""
    from alembic.config import Config

    cfg = Config(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", os.environ.get("DATABASE_URL", ""))
    return cfg


def _get_current_revision(alembic_cfg) -> str | None:
    """Get the current Alembic revision from the database."""
    from alembic.script import ScriptDirectory
    from alembic.runtime.environment import EnvironmentContext
    from alembic.config import Config
    from sqlalchemy import create_engine

    script = ScriptDirectory.from_config(alembic_cfg)
    url = alembic_cfg.get_main_option("sqlalchemy.url")
    engine = create_engine(url)

    with engine.connect() as conn:
        context = EnvironmentContext(alembic_cfg, script)
        context.configure(connection=conn)
        context.run_migrations()
        current_rev = context.get_context().get_current_revision()
    engine.dispose()
    return current_rev


def test_alembic_has_migrations(alembic_cfg):
    """There must be at least one migration to test downgrade."""
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(alembic_cfg)
    heads = script.get_heads()
    assert len(heads) > 0, "No migration heads found — nothing to downgrade from"
    print(f"\n  Migration heads: {heads}")


def test_alembic_upgrade_and_downgrade_roundtrip(alembic_cfg):
    """Apply all migrations, downgrade one step, then upgrade back.

    This validates that:
      - upgrade() creates tables/columns correctly
      - downgrade() reverses them correctly
      - chaining upgrade → downgrade → upgrade doesn't crash
    """
    from alembic import command
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    from sqlalchemy import create_engine, inspect, text

    script = ScriptDirectory.from_config(alembic_cfg)
    heads = script.get_heads()
    assert len(heads) > 0, "No migration heads"

    url = alembic_cfg.get_main_option("sqlalchemy.url")
    engine = create_engine(url)

    try:
        # ── 1. Apply all migrations ──
        print(f"\n  Applying all migrations (upgrade head)...")
        command.upgrade(alembic_cfg, "head")

        # Capture the list of tables after upgrade
        with engine.connect() as conn:
            inspector = inspect(conn)
            tables_after_upgrade = set(inspector.get_table_names())
        print(f"  Tables after upgrade ({len(tables_after_upgrade)}): {sorted(tables_after_upgrade)}")

        # ── 2. Get current revision ──
        current_head = script.get_heads()[0]

        # ── 3. Downgrade by one revision ──
        print(f"  Downgrading from {current_head} by one revision...")
        # Find the parent revision of the head
        head_revision = script.get_revision(current_head)
        downgrade_target = head_revision.down_revision
        assert downgrade_target is not None, (
            f"Head revision {current_head} has no down_revision — cannot downgrade"
        )

        command.downgrade(alembic_cfg, downgrade_target)

        # Capture tables after downgrade
        with engine.connect() as conn:
            inspector = inspect(conn)
            tables_after_downgrade = set(inspector.get_table_names())
        print(f"  Tables after downgrade ({len(tables_after_downgrade)}): {sorted(tables_after_downgrade)}")

        # Tables should have changed (downgrade removed some)
        assert tables_after_downgrade != tables_after_upgrade, (
            "Downgrade did not change the schema — the migration may be a no-op"
        )

        # ── 4. Upgrade back to head ──
        print(f"  Upgrading back to head...")
        command.upgrade(alembic_cfg, current_head)

        # Verify tables match the original state
        with engine.connect() as conn:
            inspector = inspect(conn)
            tables_after_roundtrip = set(inspector.get_table_names())
        assert tables_after_roundtrip == tables_after_upgrade, (
            f"Round-trip mismatch:\n"
            f"  After upgrade: {sorted(tables_after_upgrade)}\n"
            f"  After roundtrip: {sorted(tables_after_roundtrip)}"
        )
        print(f"  ✅ Round-trip verified — tables match after upgrade → downgrade → upgrade")

    finally:
        engine.dispose()


def test_alembic_downgrade_all_the_way(alembic_cfg):
    """Downgrade all the way to base, then upgrade back to head.

    This is the most thorough test — it validates the entire migration chain
    is reversible.
    """
    from alembic import command
    from sqlalchemy import create_engine, inspect

    url = alembic_cfg.get_main_option("sqlalchemy.url")
    engine = create_engine(url)

    try:
        # ── First, ensure we're at head ──
        print(f"\n  Ensuring at head...")
        command.upgrade(alembic_cfg, "head")

        with engine.connect() as conn:
            inspector = inspect(conn)
            full_schema_tables = set(inspector.get_table_names())
        print(f"  Full schema tables ({len(full_schema_tables)}): {sorted(full_schema_tables)}")

        # ── Downgrade to base ──
        print(f"  Downgrading to base...")
        command.downgrade(alembic_cfg, "base")

        with engine.connect() as conn:
            inspector = inspect(conn)
            base_tables = set(inspector.get_table_names())
        print(f"  Tables at base ({len(base_tables)}): {sorted(base_tables)}")

        # ── Upgrade back to head ──
        print(f"  Upgrading back to head...")
        command.upgrade(alembic_cfg, "head")

        with engine.connect() as conn:
            inspector = inspect(conn)
            final_tables = set(inspector.get_table_names())
        assert final_tables == full_schema_tables, (
            f"Full downgrade → upgrade roundtrip mismatch"
        )
        print(f"  ✅ Full round-trip verified — base → head → base → head")

    finally:
        engine.dispose()
