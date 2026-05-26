"""normalize users.phone to E.164 (phone-only auth · Task B2)

Revision ID: 202605260900
Revises: 202605220900
Create Date: 2026-05-26 09:00:00

One-shot data migration. Rewrites every existing ``users.phone`` value
into canonical E.164 form (``+234XXXXXXXXXX``) via
``app.utils.phone.normalize_to_e164``.

Why now: Sprint 6 (phone-only auth) treats ``users.phone`` as the
primary login identifier. The login + cold-start-PIN paths look up the
user by an *exact* string match on the phone column, so legacy rows
still stored as ``08...`` or ``234...`` would silently fail to log in
once mobile starts sending the normalised form. Backfill before the
endpoint switch.

Idempotency: the query filters with ``WHERE phone NOT LIKE '+%'`` so
rows already in E.164 are skipped at the SQL level. Re-running this
migration on a fully-migrated DB does zero writes — safe for retries
and safe to run a second time on an environment where someone else
already partially backfilled.

Corrupt rows: any phone that ``normalize_to_e164`` can't parse is
logged to stdout (which the container ships to the migration job log)
and skipped. The migration intentionally does not fail the whole
upgrade on a single bad row — phone data older than 2024 is known to
include a handful of malformed entries (test seed data, manual edits)
and we'd rather complete the migration and flag those rows for manual
cleanup than refuse to upgrade at all. The DBA reviews the log after
the run.

Lock duration: row-by-row UPDATE inside the migration transaction. On
a table with <100k rows (Sprint 6 production scale) this completes in
seconds; the table-level lock is the per-row ``RowExclusiveLock``
that any UPDATE takes, not an ``AccessExclusiveLock``, so concurrent
reads are unaffected. If this table grows past ~1M rows in future,
revisit and chunk the backfill.

Downgrade: intentionally a no-op. The original local/international
mix (``08...``, ``234...``) is not reconstructible from ``+234...``
without per-row history, and there is no business reason to
de-normalise — the rest of the app is moving to E.164. Documented as
lossy/irreversible in the function docstring.
"""
from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import text

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "202605260900"
down_revision: str | None = "202605220900"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Normalise existing users.phone values to E.164 in place.

    Idempotent: rows already starting with ``+`` are skipped at the
    SQL filter, so re-running this migration on an already-migrated
    DB is a zero-write no-op.

    Corrupt phones (anything ``normalize_to_e164`` can't parse) are
    logged and skipped — they need manual cleanup, but we don't fail
    the whole migration on one bad row.
    """
    # Imported inside the function so that loading this migration's
    # module (e.g. by `alembic history`) doesn't require the app
    # package to be importable. The actual upgrade run always has the
    # app on sys.path because alembic/env.py adds it.
    from app.utils.phone import InvalidPhoneFormat, normalize_to_e164

    conn = op.get_bind()
    rows = conn.execute(
        text("SELECT id, phone FROM users WHERE phone NOT LIKE '+%'")
    ).fetchall()
    for row in rows:
        try:
            new = normalize_to_e164(row.phone)
        except InvalidPhoneFormat:
            print(
                "[migration normalize_phone_e164] skipped corrupt phone "
                f"user_id={row.id} phone={row.phone!r}"
            )
            continue
        conn.execute(
            text("UPDATE users SET phone = :p WHERE id = :id"),
            {"p": new, "id": row.id},
        )


def downgrade() -> None:
    """Irreversible — normalisation is forward-only.

    The original input format (``08...`` vs ``234...`` vs
    ``+234...``) is not stored anywhere after this migration, and
    there is no business reason to downgrade. Left as a no-op so
    ``alembic downgrade`` doesn't crash on a hypothetical schema
    rollback that targets a revision before this one — the column
    is left in E.164 form, which every prior revision tolerates
    (the column is just a ``String``).
    """
    # Intentionally empty. See docstring.
