"""Integration test for the phone-to-E.164 Alembic data migration
(``alembic/versions/202605260900_normalize_phone_e164.py``).

Drives the migration's ``upgrade()`` function directly against an
isolated in-memory SQLite engine that's been pre-seeded with a minimal
``users`` table. This mirrors the pattern in ``test_migration_5c.py`` —
unit tests don't run against a real Postgres, they validate the
migration's contract via SQLAlchemy's dialect-agnostic ``MigrationContext``.

Three behaviours are pinned:
1. Non-E.164 phones (``08...``, ``234...``) are rewritten in place.
2. Rows already in E.164 (``+234...``) are untouched (idempotence at
   the SQL filter level).
3. Re-running the migration is a no-op (idempotence end-to-end).
4. A corrupt phone in the same batch is skipped, not fatal — the
   surrounding rows still get rewritten.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Column, MetaData, String, Table, create_engine, text

MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "alembic"
    / "versions"
    / "202605260900_normalize_phone_e164.py"
)


def _load_migration_module():
    spec = importlib.util.spec_from_file_location("_mig_phone_e164", MIGRATION_PATH)
    assert spec and spec.loader, "migration file not found"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_engine_with_users():
    """In-memory SQLite engine + a minimal ``users`` table.

    Only ``id`` and ``phone`` matter to this migration — the real
    production table has many more columns, but neither the SELECT
    nor the UPDATE this migration runs touches anything else. Keeping
    the stub minimal also keeps the test free of unrelated NOT NULL
    constraints from the real schema (full_name, password_hash, etc.).
    """
    engine = create_engine("sqlite:///:memory:", future=True)
    metadata = MetaData()
    Table(
        "users",
        metadata,
        Column("id", String, primary_key=True),
        Column("phone", String, nullable=False),
    )
    metadata.create_all(engine)
    return engine


def _seed(engine, rows: list[tuple[str, str]]) -> None:
    """Insert (id, phone) pairs directly via SQL — no ORM normalisation."""
    with engine.begin() as conn:
        for uid, phone in rows:
            conn.execute(
                text("INSERT INTO users (id, phone) VALUES (:id, :p)"),
                {"id": uid, "p": phone},
            )


def _run_upgrade(engine) -> None:
    """Execute the migration's upgrade() under a real MigrationContext.

    This is the same pattern used by ``test_migration_5c.py``: the
    migration calls ``op.get_bind()`` and ``op.execute(...)``, both of
    which need a live ``Operations.context`` to be active.
    """
    migration = _load_migration_module()
    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            migration.upgrade()


def _phone_for(engine, uid: str) -> str:
    with engine.begin() as conn:
        return conn.execute(
            text("SELECT phone FROM users WHERE id = :id"), {"id": uid}
        ).scalar_one()


def test_migration_normalises_non_e164_phones_in_place():
    """Local (``08...``) and international (``234...``) phones get rewritten."""
    engine = _make_engine_with_users()
    _seed(engine, [
        ("u-local", "08011111111"),
        ("u-intl", "2348022222222"),
    ])

    _run_upgrade(engine)

    assert _phone_for(engine, "u-local") == "+2348011111111"
    assert _phone_for(engine, "u-intl") == "+2348022222222"
    engine.dispose()


def test_migration_leaves_already_e164_rows_untouched():
    """A row already in E.164 is filtered out by the SQL WHERE clause."""
    engine = _make_engine_with_users()
    _seed(engine, [("u-e164", "+2348099999999")])

    _run_upgrade(engine)

    assert _phone_for(engine, "u-e164") == "+2348099999999"
    engine.dispose()


def test_migration_is_idempotent():
    """Running upgrade twice on the same DB leaves phones unchanged."""
    engine = _make_engine_with_users()
    _seed(engine, [
        ("u-local", "08011111111"),
        ("u-e164", "+2348099999999"),
    ])

    _run_upgrade(engine)
    _run_upgrade(engine)  # second run — should be a no-op

    assert _phone_for(engine, "u-local") == "+2348011111111"
    assert _phone_for(engine, "u-e164") == "+2348099999999"
    engine.dispose()


def test_migration_skips_corrupt_phone_but_processes_neighbours(capsys):
    """A row with an unparseable phone is logged + skipped; siblings still convert."""
    engine = _make_engine_with_users()
    _seed(engine, [
        ("u-good", "08011111111"),
        ("u-bad", "not-a-phone-at-all"),
        ("u-other", "2348022222222"),
    ])

    _run_upgrade(engine)

    # Good rows were rewritten...
    assert _phone_for(engine, "u-good") == "+2348011111111"
    assert _phone_for(engine, "u-other") == "+2348022222222"
    # ...the corrupt row was left alone (not normalised, not deleted)...
    assert _phone_for(engine, "u-bad") == "not-a-phone-at-all"
    # ...and the migration logged the skip so a DBA can find it post-run.
    captured = capsys.readouterr()
    assert "skipped corrupt phone" in captured.out
    assert "u-bad" in captured.out

    engine.dispose()


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("08011111111", "+2348011111111"),  # local 0[78]9 prefix
        ("07022222222", "+2347022222222"),  # local 070 prefix
        ("09033333333", "+2349033333333"),  # local 090 prefix
        ("2348044444444", "+2348044444444"),  # intl with no +
    ],
)
def test_migration_handles_every_accepted_input_format(raw, expected):
    """All input shapes recognised by ``normalize_to_e164`` survive the migration."""
    engine = _make_engine_with_users()
    _seed(engine, [("u-1", raw)])

    _run_upgrade(engine)

    assert _phone_for(engine, "u-1") == expected
    engine.dispose()


def test_downgrade_is_a_safe_noop():
    """Downgrade is documented as irreversible; ensure it doesn't crash."""
    engine = _make_engine_with_users()
    _seed(engine, [("u-1", "+2348011111111")])

    migration = _load_migration_module()
    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            migration.downgrade()  # must not raise

    # Phone is untouched by the no-op downgrade.
    assert _phone_for(engine, "u-1") == "+2348011111111"
    engine.dispose()
