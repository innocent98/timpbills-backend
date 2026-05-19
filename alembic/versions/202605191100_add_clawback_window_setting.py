"""add referral clawback-window setting

Revision ID: 202605191100
Revises: 202605191000
Create Date: 2026-05-19 11:00:00.000000

Sprint 5b phase B2. The B1 migration seeded six referral config keys
into ``app_settings``; this revision adds the seventh,
``REFERRAL_CLAWBACK_WINDOW_DAYS = 7``, which controls how long the
nightly sweeper waits before settling a ``clawback_pending`` row
(spec §6 — "7-day refund window"). Split into its own revision rather
than amending the B1 migration so the B1 schema bundle stays a clean
unit and re-running this against a half-deployed env is a single
idempotent INSERT.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

# revision identifiers, used by Alembic.
revision: str = "202605191100"
down_revision: str | None = "202605191000"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_KEY = "REFERRAL_CLAWBACK_WINDOW_DAYS"
_VALUE = "7"


def upgrade() -> None:
    if context.is_offline_mode():
        op.execute(
            f"INSERT INTO app_settings (key, value, created_at, updated_at) "
            f"VALUES ('{_KEY}', '{_VALUE}', now(), now()) "
            f"ON CONFLICT (key) DO NOTHING"
        )
        return

    bind = op.get_bind()
    bind.execute(
        sa.text(
            """
            INSERT INTO app_settings (key, value, created_at, updated_at)
            VALUES (:k, :v, now(), now())
            ON CONFLICT (key) DO NOTHING
            """
        ),
        {"k": _KEY, "v": _VALUE},
    )


def downgrade() -> None:
    if context.is_offline_mode():
        op.execute(f"DELETE FROM app_settings WHERE key = '{_KEY}'")
        return
    bind = op.get_bind()
    bind.execute(
        sa.text("DELETE FROM app_settings WHERE key = :k"),
        {"k": _KEY},
    )
