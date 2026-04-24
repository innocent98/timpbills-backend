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

    def events_for(self, tx_or_id: Transaction | UUID) -> list[TransactionEvent]:
        tx_id = tx_or_id.id if isinstance(tx_or_id, Transaction) else tx_or_id
        return (
            self._db.query(TransactionEvent)
            .filter(TransactionEvent.transaction_id == tx_id)
            .order_by(TransactionEvent.created_at.asc())
            .all()
        )

    def create_refund(
        self,
        *,
        original_tx: Transaction,
        amount: Decimal,
        reason: str,
    ) -> tuple[Transaction, bool]:
        """Create a refund Transaction linked to an original failed tx.

        Idempotent by ``original_tx.reference``: if a refund row already exists
        for this original, returns it without creating a duplicate.

        Returns ``(refund, was_created)`` — callers gate wallet credits on
        ``was_created`` so a repeat call never double-credits. Critical for
        the race between webhook, reconcile, and sync-purchase paths all
        attempting to finalize the same failed tx with the same reason.
        The event-list heuristic this replaced (checking
        ``len(events_for(refund)) == 1``) silently failed when two callers
        passed identical reason strings — see S3C-P1.
        """
        # Idempotency: Python-level scan so JSON filtering works on both
        # SQLite (tests) and Postgres (production) without JSON operator differences.
        candidates = (
            self._db.query(Transaction)
            .filter(
                Transaction.user_id == original_tx.user_id,
                Transaction.type == TransactionType.refund,
            )
            .all()
        )
        for c in candidates:
            if c.meta and c.meta.get('original_reference') == original_tx.reference:
                return c, False

        # Refund refs stay alphanumeric-only after the 12-digit stamp so
        # any downstream code paths that might hand this reference to
        # VTPass (e.g. ops-initiated requery) stay compliant. See
        # app/utils/references.py for the full format spec.
        refund_ref = new_transaction_reference(
            user_id=str(original_tx.user_id), prefix='TMPR'
        )
        refund = Transaction(
            user_id=original_tx.user_id,
            reference=refund_ref,
            type=TransactionType.refund,
            status=TransactionStatus.success,
            amount=amount,
            fee=Decimal('0'),
            currency=original_tx.currency,
            meta={
                'original_reference': original_tx.reference,
                'original_type': original_tx.type.value,
            },
        )
        self._db.add(refund)
        self._db.flush()

        event = TransactionEvent(
            transaction_id=refund.id,
            from_status=None,
            to_status=TransactionStatus.success,
            reason=reason,
            context={'original_reference': original_tx.reference},
        )
        self._db.add(event)
        self._db.flush()
        return refund, True

    def get_by_reference(self, reference: str) -> Transaction | None:
        return (
            self._db.query(Transaction)
            .filter(Transaction.reference == reference)
            .first()
        )
