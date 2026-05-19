"""ReferralService — credit pipeline + state-machine helpers.

Sprint 5b B2. Implements the pipeline described in spec §5.4: row-locked,
idempotent, KYC-cap-aware. Side-effect-free outside the DB except for
push notifications, which the caller wires in via the optional ``push``
callable so tests can record them in-memory.

Single-flight guard rests on the ``referrals`` row, NOT on
``wallet_service.credit`` (which is intentionally caller-agnostic). The
``SELECT … FOR UPDATE`` lock + ``status != "pending"`` early-return is
the entire idempotency story: two concurrent first-tx-success handlers
cannot both credit the same referral.

Key behaviours per spec §6:

* Daily cap hit → ``deferred_daily_cap``; row stays ``pending``; the
  nightly sweeper re-evaluates.
* Lifetime cap hit → ``voided_lifetime_cap`` with reason
  ``lifetime_cap_exceeded``; referee still gets their ₦50 (good-faith
  participation).
* Referrer's wallet would exceed their KYC cap → ``voided_referrer_cap``
  with reason ``referrer_wallet_cap_exceeded``; referee credited; push
  fired prompting referrer to upgrade KYC.
* Referee's wallet would exceed their KYC cap → ``referee_cap_pending``;
  referrer already credited; sweeper retries once KYC is upgraded.
* Referrer is inactive → ``voided_referrer_inactive``; referee credited.
* Killswitch off (``REFERRAL_ENABLED=false``) → ``deferred_killswitch``;
  row stays ``pending``; re-enabling resumes processing.

The self-referral-by-bank check is **stubbed to return False** because
the ``Payment`` model carries only a ``bank_name`` (no account number)
today — a real check requires a Paystack bank-account model that's
out of scope for B2. Flagged in SPRINT_5B_B2_STATUS.md.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Callable, Optional
from uuid import UUID

from sqlalchemy.orm import Session

from app.core.logger import log
from app.db.models.referral import Referral, ReferralStatus
from app.db.models.user import User
from app.services.app_setting_service import AppSettingService
from app.services.wallet_service import KycCapExceeded, WalletService


# ── Public types ───────────────────────────────────────────────────────


class ReferralCreditOutcome(str, enum.Enum):
    """Result of an attempt_credit call.

    Distinct from ``ReferralStatus`` because the outcome encodes *what
    happened in this call* (including no-ops), while status encodes the
    row's terminal state. e.g. a deferred-by-daily-cap call leaves the
    row pending — the outcome is ``deferred_daily_cap`` so the caller can
    log/observe the reason without re-querying the row.
    """
    credited                       = "credited"
    referee_cap_pending            = "referee_cap_pending"
    deferred_daily_cap             = "deferred_daily_cap"
    deferred_killswitch            = "deferred_killswitch"
    voided_lifetime_cap            = "voided_lifetime_cap"
    voided_referrer_cap            = "voided_referrer_cap"
    voided_referrer_inactive       = "voided_referrer_inactive"
    voided_self_referral_bank      = "voided_self_referral_bank"
    noop_no_referral               = "noop_no_referral"
    noop_already_processed         = "noop_already_processed"


@dataclass(frozen=True, slots=True)
class ReferralCreditResult:
    """Lightweight return value from ``attempt_credit``. ``referral_id``
    is None only for ``noop_no_referral``."""
    outcome: ReferralCreditOutcome
    referral_id: Optional[UUID] = None


# ── Push side-effect contract ──────────────────────────────────────────

# Type alias for the optional push callable. The service uses keyword-only
# kwargs so the caller's adapter can ignore fields it doesn't care about.
PushFn = Callable[..., None]


# ── Void reasons (stable strings — kept in sync with spec §6 table) ───

class VoidReason:
    lifetime_cap_exceeded         = "lifetime_cap_exceeded"
    referrer_inactive             = "referrer_inactive"
    referrer_wallet_cap_exceeded  = "referrer_wallet_cap_exceeded"
    self_referral_bank_match      = "self_referral_bank_match"
    referee_deleted               = "referee_deleted"


# ── Service ────────────────────────────────────────────────────────────


class ReferralService:
    def __init__(
        self,
        *,
        db: Session,
        wallet_svc: WalletService,
        settings_svc: AppSettingService,
        push: PushFn | None = None,
    ) -> None:
        self._db = db
        self._wallet = wallet_svc
        self._settings = settings_svc
        self._push = push

    # ── Public API ───────────────────────────────────────────────────

    def attempt_credit(
        self,
        *,
        referee_user_id: UUID,
        qualifying_tx_id: UUID,
    ) -> ReferralCreditResult:
        """Run the credit pipeline for the referral row owned by this
        referee. Idempotent: a row already past ``pending`` is a no-op."""

        # ── 1. Lock the row. Single-flight guard. ────────────────────
        row = (
            self._db.query(Referral)
            .filter(Referral.referee_user_id == referee_user_id)
            .with_for_update()
            .first()
        )
        if row is None:
            return ReferralCreditResult(ReferralCreditOutcome.noop_no_referral)
        if row.status is not ReferralStatus.pending:
            return ReferralCreditResult(
                ReferralCreditOutcome.noop_already_processed,
                referral_id=row.id,
            )

        # ── 2. Killswitch — defer entirely, no state change. ─────────
        if not self._settings.get_bool("REFERRAL_ENABLED", default=True):
            self._db.commit()
            return ReferralCreditResult(
                ReferralCreditOutcome.deferred_killswitch,
                referral_id=row.id,
            )

        # ── 3. Load both users. ──────────────────────────────────────
        referrer = self._db.query(User).filter(User.id == row.referrer_user_id).first()
        referee = self._db.query(User).filter(User.id == row.referee_user_id).first()
        if referrer is None or referee is None:
            # Referee deletion is the only realistic path; FK constraint
            # would cascade and remove this row before we got here.
            # Referrer is RESTRICT, so should be present. Defensive void.
            self._void(row, VoidReason.referee_deleted)
            self._db.commit()
            return ReferralCreditResult(
                ReferralCreditOutcome.voided_referrer_inactive,  # closest bucket
                referral_id=row.id,
            )

        # ── 4. Guardrails ────────────────────────────────────────────

        # 4a. Self-referral by bank — STUBBED (see module docstring).
        if self._is_self_referral_by_bank(referrer=referrer, referee=referee):
            self._void(row, VoidReason.self_referral_bank_match)
            self._db.commit()
            return ReferralCreditResult(
                ReferralCreditOutcome.voided_self_referral_bank,
                referral_id=row.id,
            )

        # 4b. Referrer disabled/banned → void with reason, credit referee.
        if not referrer.is_active:
            self._void(row, VoidReason.referrer_inactive)
            self._credit_referee_best_effort(row)
            self._db.commit()
            return ReferralCreditResult(
                ReferralCreditOutcome.voided_referrer_inactive,
                referral_id=row.id,
            )

        # 4c. Daily cap — defer (row stays pending; sweeper retries).
        if self._daily_cap_reached(referrer_id=row.referrer_user_id):
            # Don't commit — the FOR UPDATE lock is released by the
            # rollback. Status unchanged.
            self._db.rollback()
            return ReferralCreditResult(
                ReferralCreditOutcome.deferred_daily_cap,
                referral_id=row.id,
            )

        # 4d. Lifetime cap — void, credit referee.
        if self._lifetime_cap_reached(referrer_id=row.referrer_user_id):
            self._void(row, VoidReason.lifetime_cap_exceeded)
            self._credit_referee_best_effort(row)
            self._db.commit()
            return ReferralCreditResult(
                ReferralCreditOutcome.voided_lifetime_cap,
                referral_id=row.id,
            )

        # ── 5. All clear — transition + credit. ──────────────────────
        referrer_reward = self._settings.get_decimal("REFERRAL_REWARD_REFERRER_NAIRA")
        referee_reward = self._settings.get_decimal("REFERRAL_REWARD_REFEREE_NAIRA")
        now = datetime.now(timezone.utc)

        row.status = ReferralStatus.attributed
        row.attributed_at = now
        row.qualifying_tx_id = qualifying_tx_id
        self._db.flush()

        # 5a. Credit referrer first. If their KYC cap blocks it, void
        # the row with a specific reason and still pay the referee.
        try:
            self._wallet.credit(user_id=row.referrer_user_id, amount=referrer_reward)
        except KycCapExceeded:
            self._void(row, VoidReason.referrer_wallet_cap_exceeded)
            self._credit_referee_best_effort(row)
            self._db.commit()
            self._fire_push(
                event="referral_cap_blocked",
                user_id=row.referrer_user_id,
                context={"referral_id": str(row.id)},
            )
            return ReferralCreditResult(
                ReferralCreditOutcome.voided_referrer_cap,
                referral_id=row.id,
            )

        # 5b. Credit referee. If their KYC cap blocks it, referrer is
        # already credited — mark referee_cap_pending so the sweeper can
        # retry once their KYC is upgraded.
        try:
            self._wallet.credit(user_id=row.referee_user_id, amount=referee_reward)
        except KycCapExceeded:
            row.status = ReferralStatus.referee_cap_pending
            self._db.commit()
            return ReferralCreditResult(
                ReferralCreditOutcome.referee_cap_pending,
                referral_id=row.id,
            )

        # 5c. Happy path complete.
        row.status = ReferralStatus.credited
        row.credited_at = now
        self._db.commit()

        # Post-commit side effects — pushes fired after the row + wallets
        # are durable so a notification crash never undoes the credit.
        self._fire_push(
            event="referral_credited",
            user_id=row.referrer_user_id,
            context={
                "referral_id": str(row.id),
                "amount_naira": str(referrer_reward),
            },
        )
        self._fire_push(
            event="welcome_bonus",
            user_id=row.referee_user_id,
            context={
                "referral_id": str(row.id),
                "amount_naira": str(referee_reward),
            },
        )

        return ReferralCreditResult(
            ReferralCreditOutcome.credited,
            referral_id=row.id,
        )

    # ── Helpers (public for testability of state machine) ────────────

    def transition_to(self, *, referral_id: UUID, status: ReferralStatus) -> None:
        """Move a row to a new status. Used by the sweeper for state
        transitions that don't go through ``attempt_credit`` (e.g.
        ``clawback_pending → clawed_back``)."""
        row = (
            self._db.query(Referral)
            .filter(Referral.id == referral_id)
            .with_for_update()
            .first()
        )
        if row is None:
            return
        row.status = status
        if status is ReferralStatus.clawed_back:
            row.clawed_back_at = datetime.now(timezone.utc)
        self._db.commit()

    def void(self, *, referral_id: UUID, reason: str) -> None:
        """Void a row with the given reason. Terminal."""
        row = (
            self._db.query(Referral)
            .filter(Referral.id == referral_id)
            .with_for_update()
            .first()
        )
        if row is None:
            return
        self._void(row, reason)
        self._db.commit()

    # ── Internals ────────────────────────────────────────────────────

    def _void(self, row: Referral, reason: str) -> None:
        row.status = ReferralStatus.voided
        row.void_reason = reason

    def _credit_referee_best_effort(self, row: Referral) -> None:
        """Credit referee's wallet swallowing KycCapExceeded. Used in the
        void-paths where we still want to honour the referee's bonus but
        won't escalate if their wallet is also capped — sweeper handles
        the retry via a referee_cap_pending row if needed.

        For void paths, however, the row is already terminal-voided —
        leaving the referee uncredited is acceptable since this is a
        rare edge (referrer inactive AND referee at cap). Log it loud so
        ops sees it."""
        reward = self._settings.get_decimal("REFERRAL_REWARD_REFEREE_NAIRA")
        try:
            self._wallet.credit(user_id=row.referee_user_id, amount=reward)
        except KycCapExceeded:
            log.warning(
                "referral: referee credit blocked by KYC cap in void path "
                "referral=%s referee=%s reward=%s",
                row.id, row.referee_user_id, reward,
            )

    def _is_self_referral_by_bank(self, *, referrer: User, referee: User) -> bool:
        """STUB. The Payment model carries a ``bank_name`` but no account
        number, so a real same-bank-account check is not possible today.
        Returns False so the pipeline continues; flagged in
        SPRINT_5B_B2_STATUS.md.

        TODO(sprint-5b/follow-up): wire to a Paystack bank-account model
        once one exists. Match referee's verified account_number+bank_code
        against any of referrer's verified accounts → True."""
        return False

    def _daily_cap_reached(self, *, referrer_id: UUID) -> bool:
        """True iff the referrer has hit ``REFERRAL_DAILY_CAP`` credited
        referrals in the current UTC day. Counts ``credited`` (not
        ``attributed`` mid-flight) so a deferred row doesn't burn a slot
        until it actually pays out."""
        cap = self._settings.get_int("REFERRAL_DAILY_CAP")
        if cap <= 0:
            return True
        start_of_day = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0,
        )
        credited_today = (
            self._db.query(Referral)
            .filter(
                Referral.referrer_user_id == referrer_id,
                Referral.status == ReferralStatus.credited,
                Referral.credited_at >= start_of_day,
            )
            .count()
        )
        return credited_today >= cap

    def _lifetime_cap_reached(self, *, referrer_id: UUID) -> bool:
        """True iff the referrer's lifetime ``credited`` referral earnings
        equal or exceed ``REFERRAL_LIFETIME_CAP_NAIRA``. The next credit
        is what we're trying to issue, so "equal" still trips the cap —
        if the cap is ₦50,000 and the referrer is at exactly ₦50,000,
        another credit would push them over."""
        cap = self._settings.get_decimal("REFERRAL_LIFETIME_CAP_NAIRA")
        reward = self._settings.get_decimal("REFERRAL_REWARD_REFERRER_NAIRA")
        credited_count = (
            self._db.query(Referral)
            .filter(
                Referral.referrer_user_id == referrer_id,
                Referral.status == ReferralStatus.credited,
            )
            .count()
        )
        lifetime_earned = Decimal(credited_count) * reward
        return (lifetime_earned + reward) > cap

    def _fire_push(
        self, *, event: str, user_id: UUID, context: dict
    ) -> None:
        if self._push is None:
            return
        try:
            self._push(event=event, user_id=user_id, context=context)
        except Exception as exc:  # noqa: BLE001
            # Never let a notification failure roll back a credit. The
            # row + wallets are already committed by the time we land here.
            log.warning(
                "referral: push failed event=%s user=%s err=%s",
                event, user_id, exc,
            )
