# app/services/wallet_service.py
"""Wallet service — atomic credit/debit with row-level locking and KYC cap.

Balance invariants enforced here AND at the DB level (CHECK constraints
on wallets table). Never bypass this service to mutate a wallet.
"""
import enum
from decimal import Decimal
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models._enums import SpendLockReason
from app.db.models.user import KycLevel, User
from app.db.models.wallet import Wallet
from app.db.models.wallet_credit_key import WalletCreditKey

# Naira caps per KYC tier, from PRD §13 (spec §2 — 4-tier table). Only the
# max-balance cap is enforced here; per-txn/daily limits are display-only
# and live elsewhere. tier_3 is unlimited: `None` means "no cap enforced".
_KYC_CAPS: dict[KycLevel, Decimal | None] = {
    KycLevel.tier_0: Decimal("50000.00"),
    KycLevel.tier_1: Decimal("300000.00"),
    KycLevel.tier_2: Decimal("500000.00"),
    KycLevel.tier_3: None,
}

# `balance_cap` is NOT NULL, NUMERIC(14,2) (12 integer digits + 2 decimal),
# so the absolute max representable value is 999_999_999_999.99. This
# sentinel is written for unlimited (tier_3) wallets — comfortably above
# any real balance, with headroom below the column's hard ceiling so we
# never sit exactly at the edge of precision.
_UNLIMITED_CAP = Decimal("900000000000.00")


def _resolve_cap(kyc_level: KycLevel) -> Decimal | None:
    """Cap for a KYC tier, or None if the tier is unlimited (tier_3)."""
    return _KYC_CAPS[kyc_level]


class InsufficientBalance(Exception):
    pass


class KycCapExceeded(Exception):
    pass


class WalletNotFound(Exception):
    pass


class OverCapPolicy(str, enum.Enum):
    """How credit() reacts when a credit would push balance past the KYC cap.

    RAISE  - checkout path (default). Raise KycCapExceeded; the caller returns
             422 and Paystack retries until ops raises the tier. Unchanged.
    LOCK   - transfer/DVA path. Landed money is never rejected: credit in full
             and lock outbound spend until the next KYC upgrade covers it.
    """
    RAISE = "raise"
    LOCK = "lock"


class WalletSpendLocked(Exception):
    """Outbound money-move attempted while the wallet is spend-locked."""


class WalletService:
    def __init__(self, *, db: Session) -> None:
        self._db = db

    def get_or_create(self, *, user_id: UUID) -> Wallet:
        w = self._db.query(Wallet).filter(Wallet.user_id == user_id).first()
        if w is None:
            user = self._db.query(User).filter(User.id == user_id).first()
            cap = _resolve_cap(user.kyc_level) if user else Decimal("50000.00")
            w = Wallet(
                user_id=user_id,
                balance=Decimal("0.00"),
                balance_cap=cap if cap is not None else _UNLIMITED_CAP,
            )
            self._db.add(w)
            self._db.commit()
            self._db.refresh(w)
        return w

    def balance(self, *, user_id: UUID) -> Decimal:
        w = self.get_or_create(user_id=user_id)
        return w.balance

    def credit(
        self,
        *,
        user_id: UUID,
        amount: Decimal,
        over_cap: OverCapPolicy = OverCapPolicy.RAISE,
        idempotency_key: str | None = None,
    ) -> Decimal:
        """Credit atomically under SELECT … FOR UPDATE.

        ``over_cap`` selects the over-cap behaviour (see OverCapPolicy).
        Default RAISE keeps the checkout path identical to before.

        ``idempotency_key`` makes the credit exactly-once. When supplied, a
        uniquely-constrained ``wallet_credit_keys`` marker is reserved in the
        SAME database transaction as the balance mutation below. A repeat call
        with the same key collides on the unique constraint and becomes a safe
        no-op that returns the current balance WITHOUT crediting again. This is
        the no-double-credit guard for the DVA funding path: the live webhook
        and the reconciliation sweep both pass ``tx.reference``, so whichever
        credits first wins and the other is a no-op. Callers that omit the key
        (checkout, refund credits) keep their previous behaviour unchanged.
        """
        w = (
            self._db.query(Wallet)
            .filter(Wallet.user_id == user_id)
            .with_for_update()
            .first()
        )
        if w is None:
            w = self.get_or_create(user_id=user_id)
            w = (
                self._db.query(Wallet)
                .filter(Wallet.id == w.id)
                .with_for_update()
                .first()
            )

        # Refresh cap from user's current KYC level. `cap is None` means
        # unlimited (tier_3) — no user record is the only case that falls
        # back to the wallet's already-stored cap.
        user = self._db.query(User).filter(User.id == user_id).first()
        if user is not None:
            cap = _resolve_cap(user.kyc_level)
            w.balance_cap = cap if cap is not None else _UNLIMITED_CAP
        else:
            cap = w.balance_cap

        # Reserve the idempotency marker AFTER the wallet is locked and BEFORE
        # the balance mutation, so it commits atomically with the balance
        # change at the single self._db.commit() below. A collision means this
        # exact credit already landed: roll back (nothing was mutated) and
        # return the current balance as a no-op.
        if idempotency_key is not None:
            self._db.add(
                WalletCreditKey(key=idempotency_key, user_id=user_id, amount=amount)
            )
            try:
                self._db.flush()
            except IntegrityError:
                self._db.rollback()
                existing = (
                    self._db.query(Wallet)
                    .filter(Wallet.user_id == user_id)
                    .first()
                )
                return existing.balance if existing is not None else Decimal("0.00")

        new_balance = w.balance + amount
        if cap is not None and new_balance > cap:
            if over_cap == OverCapPolicy.RAISE:
                raise KycCapExceeded(
                    f"new balance {new_balance} exceeds cap {cap}"
                )
            # LOCK: credit in full, never reject landed money; gate outbound.
            w.balance = new_balance
            w.spend_locked = True
            w.spend_locked_reason = SpendLockReason.over_cap
        else:
            w.balance = new_balance
        self._db.commit()
        return new_balance

    def debit(self, *, user_id: UUID, amount: Decimal) -> Decimal:
        """Debit atomically. Raises InsufficientBalance."""
        w = (
            self._db.query(Wallet)
            .filter(Wallet.user_id == user_id)
            .with_for_update()
            .first()
        )
        if w is None or w.balance < amount:
            raise InsufficientBalance()
        w.balance = w.balance - amount
        self._db.commit()
        return w.balance

    def raise_if_spend_locked(self, *, user_id: UUID) -> None:
        """Guard for every outbound money-move. Raises WalletSpendLocked when
        the wallet is locked (over-cap landed money awaiting a KYC upgrade)."""
        w = self._db.query(Wallet).filter(Wallet.user_id == user_id).first()
        if w is not None and w.spend_locked:
            raise WalletSpendLocked(
                "wallet is spend-locked pending a KYC upgrade"
            )

    def clear_spend_lock_if_within_cap(self, *, user_id: UUID) -> bool:
        """Clear an over-cap spend-lock when the user's current KYC cap now
        covers the balance. Called after a tier upgrade. Returns True if the
        lock was cleared, False if it was left in place or absent."""
        w = (
            self._db.query(Wallet)
            .filter(Wallet.user_id == user_id)
            .with_for_update()
            .first()
        )
        if (
            w is None
            or not w.spend_locked
            or w.spend_locked_reason != SpendLockReason.over_cap
        ):
            return False
        user = self._db.query(User).filter(User.id == user_id).first()
        cap = _resolve_cap(user.kyc_level) if user is not None else w.balance_cap
        if cap is None or w.balance <= cap:
            w.spend_locked = False
            w.spend_locked_reason = None
            self._db.commit()
            return True
        return False
