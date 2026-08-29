"""normalize users.email (lowercase) + citext for case-insensitive uniqueness

Revision ID: 202608291200
Revises: 202607251200
Create Date: 2026-08-29 12:00:00

Email is case-insensitive in practice (the domain always, and every
real-world provider treats the local-part so too). Historically
``users.email`` was a plain case-sensitive ``String`` unique index and
``EmailStr`` never lowercased, so ``Example@X.com`` and ``example@x.com``
could become two distinct accounts and cross-case lookups silently
missed. This migration canonicalises storage and makes the unique index
case-insensitive.

Postgres-only. Steps:
  1. CREATE EXTENSION IF NOT EXISTS citext;
  2. GUARD: refuse to run if case-insensitive duplicate emails exist
     (ops must merge them first — we never silently drop rows).
  3. UPDATE users SET email = lower(email);  (normalise existing storage)
  4. ALTER COLUMN email TYPE citext;         (recreates the unique index
     case-insensitively — two rows differing only by case now collide).

On non-Postgres backends (SQLite in tests) this is a no-op: there is no
citext, and case-insensitive collision is prevented at the boundary by
``app.utils.email.normalize_email`` (schema validators + service-layer
defense-in-depth). The SQLAlchemy model column stays ``String`` for
SQLite portability.

Downgrade: alter the column type back to VARCHAR. The lowercased values
are left as-is (forward-only normalisation; the original casing is not
recoverable and there is no reason to restore it).
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy import text

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "202608291200"
down_revision: str | None = "202607251200"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # SQLite / other: normalisation is enforced at the app boundary.
        return

    # 1. citext extension (idempotent).
    op.execute("CREATE EXTENSION IF NOT EXISTS citext;")

    # 2. Guard: block the migration if case-insensitive duplicates exist so
    #    ops can merge accounts before retrying. We do NOT silently drop data.
    dupes = bind.execute(
        text(
            "SELECT lower(email) AS e, count(*) AS n "
            "FROM users GROUP BY lower(email) HAVING count(*) > 1"
        )
    ).fetchall()
    if dupes:
        listing = ", ".join(f"{row.e} (x{row.n})" for row in dupes)
        raise RuntimeError(
            "Cannot normalise users.email: case-insensitive duplicate "
            f"addresses exist and must be merged first: {listing}"
        )

    # 3. Normalise existing storage to lowercase.
    op.execute("UPDATE users SET email = lower(email);")

    # 4. Switch the column to citext — this recreates the unique index
    #    case-insensitively.
    op.execute("ALTER TABLE users ALTER COLUMN email TYPE citext;")


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    op.alter_column(
        "users",
        "email",
        type_=sa.String(),
        existing_nullable=False,
        postgresql_using="email::varchar",
    )
