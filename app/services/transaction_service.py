"""Transaction service — creates transactions, enforces state transitions,
and writes an audit row for every transition."""
from decimal import Decimal
from typing import Iterable
from uuid import UUID

from sqlalchemy.orm import Session

from app.db.models._enums import TransactionStatus, TransactionType
from app.db.models.transaction import Transaction
from app.db.models.transaction_event import TransactionEvent
from app.utils.references import new_transaction_reference


class InvalidStateTransition(Exception):
    pass


# Legal transitions. Anything not listed raises InvalidStateTransition.
_ALLOWED: dict[TransactionStatus, set[TransactionStatus]] = {
    TransactionStatus.pending: {
        TransactionStatus.processing,
        TransactionStatus.success,
        TransactionStatus.failed,
    },
    TransactionStatus.processing: {
        TransactionStatus.success,
        TransactionStatus.failed,
    },
    TransactionStatus.success: {
        TransactionStatus.refund_pending,
    },
    TransactionStatus.failed: {
        TransactionStatus.refund_pending,
    },
    TransactionStatus.refund_pending: {
        TransactionStatus.refunded,
        TransactionStatus.refund_failed,
    },
    # Terminal states
    TransactionStatus.refunded: set(),
    TransactionStatus.refund_failed: set(),
}


class TransactionService:
    def __init__(self, *, db: Session) -> None:
        self._db = db

    def create(
        self,
        *,
        user_id: str | UUID,
        type: TransactionType,
        amount: Decimal,
        fee: Decimal = Decimal("0.00"),
        meta: dict | None = None,
    ) -> Transaction:
        tx = Transaction(
            user_id=user_id if isinstance(user_id, UUID) else UUID(user_id),
            reference=new_transaction_reference(user_id=str(user_id)),
            type=type,
            status=TransactionStatus.pending,
            amount=amount,
            fee=fee,
            meta=meta or {},
        )
        self._db.add(tx)
        self._db.commit()
        self._db.refresh(tx)
        return tx

    def transition(
        self,
        tx: Transaction,
        *,
        to_status: TransactionStatus,
        reason: str | None = None,
        context: dict | None = None,
    ) -> None:
        if tx.status == to_status:
            return  # idempotent no-op
        if to_status not in _ALLOWED.get(tx.status, set()):
            raise InvalidStateTransition(
                f"{tx.status.value} → {to_status.value} is not allowed"
            )
        event = TransactionEvent(
            transaction_id=tx.id,
            from_status=tx.status,
            to_status=to_status,
            reason=reason,
            context=context or {},
        )
        tx.status = to_status
        self._db.add(event)
        self._db.commit()

    def events_for(self, tx_id: UUID) -> list[TransactionEvent]:
        return (
            self._db.query(TransactionEvent)
            .filter(TransactionEvent.transaction_id == tx_id)
            .order_by(TransactionEvent.created_at.asc())
            .all()
        )

    def get_by_reference(self, reference: str) -> Transaction | None:
        return (
            self._db.query(Transaction)
            .filter(Transaction.reference == reference)
            .first()
        )
