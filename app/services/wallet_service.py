# app/services/wallet_service.py
"""Wallet service — atomic credit/debit with row-level locking and KYC cap.

Balance invariants enforced here AND at the DB level (CHECK constraints
on wallets table). Never bypass this service to mutate a wallet.
"""
from decimal import Decimal
from uuid import UUID

from sqlalchemy.orm import Session

from app.db.models.user import KycLevel, User
from app.db.models.wallet import Wallet

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

    def credit(self, *, user_id: UUID, amount: Decimal) -> Decimal:
        """Credit atomically under SELECT … FOR UPDATE. Raises KycCapExceeded."""
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

        new_balance = w.balance + amount
        if cap is not None and new_balance > cap:
            raise KycCapExceeded(
                f"new balance {new_balance} exceeds cap {cap}"
            )
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
