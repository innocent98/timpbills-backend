"""Tests for the Sprint 5c profile-fields + notification_preferences
migration (``alembic/versions/202605201200_sprint_5c_profile_fields.py``).

We don't run `alembic upgrade` against a real Postgres in unit tests. Instead
we drive the migration's ``upgrade()`` function directly against an isolated
in-memory SQLite engine that's been pre-seeded with a minimal ``users`` table
matching the columns the migration expects to extend. This validates the
migration's DDL contract (columns, table, indexes, unique constraint) without
depending on later-task ORM edits and without needing Postgres on the test
runner.

Notes:
- The migration uses ``server_default=sa.text("gen_random_uuid()")`` which is
  Postgres-only — SQLite happily accepts it as a literal expression at DDL
  parse time but would fail at INSERT. We only inspect schema here, never
  INSERT into the new table, so the difference is invisible.
- The downgrade test verifies symmetry: every artefact created by upgrade is
  cleanly removed.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Column, MetaData, String, Table, create_engine, inspect
from sqlalchemy.dialects.postgresql import UUID

MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "alembic"
    / "versions"
    / "202605201200_sprint_5c_profile_fields.py"
)


def _load_migration_module():
    spec = importlib.util.spec_from_file_location("_mig_5c", MIGRATION_PATH)
    assert spec and spec.loader, "migration file not found"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def migrated_engine():
    """SQLite engine with a stub ``users`` table, then the 5c upgrade applied.

    Yields an engine the test can `inspect()`. Symmetry-check downgrade is in
    a separate test so a failure there can't mask an upgrade-shape failure.
    """
    engine = create_engine("sqlite:///:memory:", future=True)
    # Minimal stand-in for users — only what the migration's ForeignKey needs.
    metadata = MetaData()
    Table(
        "users",
        metadata,
        Column("id", UUID(as_uuid=True), primary_key=True),
        Column("email", String, nullable=False),
    )
    metadata.create_all(engine)

    migration = _load_migration_module()
    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            migration.upgrade()

    try:
        yield engine
    finally:
        engine.dispose()


def test_user_table_has_sprint_5c_columns(migrated_engine):
    inspector = inspect(migrated_engine)
    cols = {c["name"] for c in inspector.get_columns("users")}
    assert {"date_of_birth", "gender", "address", "avatar_url"}.issubset(cols)


def test_notification_preferences_table_exists(migrated_engine):
    inspector = inspect(migrated_engine)
    assert "notification_preferences" in inspector.get_table_names()
    cols = {c["name"] for c in inspector.get_columns("notification_preferences")}
    assert cols == {
        "id",
        "user_id",
        "transaction_alerts",
        "referral_updates",
        "promotions",
        "email_notifications",
        "created_at",
        "updated_at",
    }


def test_notification_preferences_user_id_is_unique(migrated_engine):
    inspector = inspect(migrated_engine)
    uniques = inspector.get_unique_constraints("notification_preferences")
    # ``unique=True`` on a Column emits a UNIQUE constraint, which SQLAlchemy's
    # SQLite inspector reports under either get_unique_constraints or via a
    # unique=True index. Accept either shape so the test isn't dialect-brittle.
    indexes = inspector.get_indexes("notification_preferences")
    via_constraint = any("user_id" in u["column_names"] for u in uniques)
    via_unique_index = any(
        i.get("unique") and i["column_names"] == ["user_id"] for i in indexes
    )
    assert via_constraint or via_unique_index


def test_notification_preferences_user_id_indexed(migrated_engine):
    inspector = inspect(migrated_engine)
    indexes = inspector.get_indexes("notification_preferences")
    assert any(
        i["name"] == "ix_notification_preferences_user_id"
        and i["column_names"] == ["user_id"]
        for i in indexes
    )


def test_downgrade_reverses_upgrade():
    """upgrade then downgrade leaves the schema as it was before."""
    engine = create_engine("sqlite:///:memory:", future=True)
    metadata = MetaData()
    Table(
        "users",
        metadata,
        Column("id", UUID(as_uuid=True), primary_key=True),
        Column("email", String, nullable=False),
    )
    metadata.create_all(engine)

    migration = _load_migration_module()
    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            migration.upgrade()
            migration.downgrade()

    inspector = inspect(engine)
    assert "notification_preferences" not in inspector.get_table_names()
    user_cols = {c["name"] for c in inspector.get_columns("users")}
    for new_col in ("date_of_birth", "gender", "address", "avatar_url"):
        assert new_col not in user_cols, f"downgrade left {new_col} behind"

    engine.dispose()
