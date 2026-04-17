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


# Naira caps per KYC tier, from PRD §13.
_KYC_CAPS: dict[KycLevel, Decimal] = {
    KycLevel.tier_0: Decimal("50000.00"),
    KycLevel.tier_1: Decimal("200000.00"),
    KycLevel.tier_2: Decimal("500000.00"),
}


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
            cap = _KYC_CAPS[user.kyc_level] if user else Decimal("50000.00")
            w = Wallet(user_id=user_id, balance=Decimal("0.00"), balance_cap=cap)
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

        # Refresh cap from user's current KYC level
        user = self._db.query(User).filter(User.id == user_id).first()
        if user is not None:
            w.balance_cap = _KYC_CAPS[user.kyc_level]

        new_balance = w.balance + amount
        if new_balance > w.balance_cap:
            raise KycCapExceeded(
                f"new balance {new_balance} exceeds cap {w.balance_cap}"
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
