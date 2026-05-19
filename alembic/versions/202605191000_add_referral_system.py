"""add referral system: users columns, referrals table, app_settings, backfill

Revision ID: 202605191000
Revises: 202604281200
Create Date: 2026-05-19 10:00:00.000000

Sprint 5b phase B1. This migration is structural + a one-shot data
backfill in a single revision so an env going through `alembic upgrade
head` ends with every existing user holding a unique referral_code (the
column is NOT NULL once we're done).

Order of operations matters:

  1. Create the new enum + tables that have no dependency on a NOT NULL
     referral_code (referrals, app_settings).
  2. Add referral_code to users as NULLABLE, then backfill in Python via
     the helper so the same uniqueness + ambiguous-char rules apply for
     existing users as for new signups.
  3. Add the UNIQUE index, then ALTER the column to NOT NULL.
  4. Seed the 6 referral config keys into app_settings.

Downgrade tears down everything in reverse. The enum is dropped last
because columns still reference it during the table drop.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects import postgresql

from app.services.referral_code import generate_referral_code

# revision identifiers, used by Alembic.
revision: str = "202605191000"
down_revision: str | None = "202604281200"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_REFERRAL_STATUS_VALUES = (
    "pending",
    "attributed",
    "credited",
    "referee_cap_pending",
    "clawback_pending",
    "clawed_back",
    "voided",
)

# Spec §4.1 — 6 runtime-tunable keys for the referral system. Values are
# stored as strings; callers cast to int/bool at read time. Keep these in
# sync with the design spec — if you add a key here, document it there.
_DEFAULT_APP_SETTINGS: tuple[tuple[str, str], ...] = (
    ("REFERRAL_ENABLED", "true"),
    ("REFERRAL_REWARD_REFERRER_NAIRA", "100"),
    ("REFERRAL_REWARD_REFEREE_NAIRA", "50"),
    ("REFERRAL_DAILY_CAP", "5"),
    ("REFERRAL_LIFETIME_CAP_NAIRA", "50000"),
    ("REFERRAL_MIN_TX_AMOUNT_NAIRA", "1000"),
)


def upgrade() -> None:
    bind = op.get_bind()

    # ── 1. referral_status enum ────────────────────────────────────────
    referral_status_enum = postgresql.ENUM(
        *_REFERRAL_STATUS_VALUES,
        name="referral_status_enum",
        create_type=True,
    )
    referral_status_enum.create(bind, checkfirst=True)

    # ── 2. app_settings table ──────────────────────────────────────────
    op.create_table(
        "app_settings",
        sa.Column("key", sa.String(length=128), primary_key=True),
        sa.Column("value", sa.String(), nullable=False),
        sa.Column(
            "updated_by",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    # ── 3. users.referral_code (NULLABLE first) + referred_by_user_id ──
    op.add_column(
        "users",
        sa.Column("referral_code", sa.String(length=8), nullable=True),
    )
    op.add_column(
        "users",
        sa.Column(
            "referred_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_users_referred_by_user_id",
        "users",
        ["referred_by_user_id"],
    )

    # ── 4. Backfill existing users with unique codes ───────────────────
    # We hold the set of already-issued codes in Python so each
    # generate_referral_code call sees the full picture without a SELECT
    # round-trip per user. Safe at any realistic user count (<1M); above
    # that this loop becomes the bottleneck and a chunked SELECT pattern
    # would be warranted.
    #
    # Offline mode (``alembic upgrade head --sql``) has no live bind, so
    # we skip the backfill there. The DDL still gets emitted so ops can
    # review the plan; the backfill happens for real when the migration
    # runs online against a live DB.
    if not context.is_offline_mode():
        existing_codes: set[str] = set()
        user_ids = bind.execute(sa.text("SELECT id FROM users")).scalars().all()
        for user_id in user_ids:
            code = generate_referral_code(
                code_exists=lambda candidate: candidate in existing_codes
            )
            existing_codes.add(code)
            bind.execute(
                sa.text("UPDATE users SET referral_code = :code WHERE id = :uid"),
                {"code": code, "uid": user_id},
            )

    # ── 5. Tighten the column: UNIQUE index, then NOT NULL ─────────────
    op.create_index(
        "ix_users_referral_code",
        "users",
        ["referral_code"],
        unique=True,
    )
    op.alter_column("users", "referral_code", nullable=False)

    # ── 6. referrals table ─────────────────────────────────────────────
    op.create_table(
        "referrals",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
        ),
        sa.Column(
            "referrer_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "referee_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("code_used", sa.String(length=8), nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM(
                *_REFERRAL_STATUS_VALUES,
                name="referral_status_enum",
                create_type=False,
            ),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("void_reason", sa.String(length=64), nullable=True),
        sa.Column(
            "qualifying_tx_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("transactions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("attributed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("credited_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("clawed_back_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("referee_user_id", name="uq_referrals_referee"),
    )
    op.create_index(
        "ix_referrals_referrer_user_id",
        "referrals",
        ["referrer_user_id"],
    )
    op.create_index(
        "ix_referrals_referrer_status",
        "referrals",
        ["referrer_user_id", "status"],
    )
    op.create_index(
        "ix_referrals_status_created",
        "referrals",
        ["status", "created_at"],
    )

    # ── 7. Seed default app_settings rows ──────────────────────────────
    # ON CONFLICT DO NOTHING so re-running the migration on a partially
    # seeded DB (e.g. someone hand-poked the settings table) is safe.
    # Offline mode emits the INSERTs via op.execute so they appear in the
    # generated SQL plan; online uses bind.execute with parameter binding.
    for key, value in _DEFAULT_APP_SETTINGS:
        if context.is_offline_mode():
            op.execute(
                f"INSERT INTO app_settings (key, value, created_at, updated_at) "
                f"VALUES ('{key}', '{value}', now(), now()) "
                f"ON CONFLICT (key) DO NOTHING"
            )
        else:
            bind.execute(
                sa.text(
                    """
                    INSERT INTO app_settings (key, value, created_at, updated_at)
                    VALUES (:k, :v, now(), now())
                    ON CONFLICT (key) DO NOTHING
                    """
                ),
                {"k": key, "v": value},
            )


def downgrade() -> None:
    op.drop_index("ix_referrals_status_created", table_name="referrals")
    op.drop_index("ix_referrals_referrer_status", table_name="referrals")
    op.drop_index("ix_referrals_referrer_user_id", table_name="referrals")
    op.drop_table("referrals")

    op.drop_index("ix_users_referral_code", table_name="users")
    op.drop_index("ix_users_referred_by_user_id", table_name="users")
    op.drop_column("users", "referred_by_user_id")
    op.drop_column("users", "referral_code")

    op.drop_table("app_settings")

    referral_status_enum = postgresql.ENUM(
        *_REFERRAL_STATUS_VALUES,
        name="referral_status_enum",
        create_type=False,
    )
    referral_status_enum.drop(op.get_bind(), checkfirst=True)
