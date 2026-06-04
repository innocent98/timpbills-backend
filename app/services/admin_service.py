"""Read-side queries for the admin dashboard. Endpoints stay thin; all
aggregation + listing logic lives here. Computes ONLY metrics backed by
real data — no avg-processing-time (not persisted), no flight metrics."""
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db.models._enums import TransactionStatus, TransactionType
from app.db.models.payment import Payment
from app.db.models.transaction import Transaction
from app.db.models.transaction_event import TransactionEvent
from app.db.models.user import User

_SUCCESS = TransactionStatus.success
_AWAITING = (TransactionStatus.refund_pending, TransactionStatus.refund_failed)
_PENDING = (TransactionStatus.pending, TransactionStatus.processing)


class AdminService:
    def __init__(self, *, db: Session) -> None:
        self._db = db

    def overview(self, *, days: int = 7) -> dict:
        now = datetime.now(UTC)
        since = now - timedelta(days=days)
        q = self._db.query(Transaction).filter(Transaction.created_at >= since)

        total = q.count()
        success_count = q.filter(Transaction.status == _SUCCESS).count()
        success_rate = round(success_count / total, 4) if total else 0.0

        volume = (
            self._db.query(func.coalesce(func.sum(Transaction.amount), 0))
            .filter(Transaction.created_at >= since, Transaction.status == _SUCCESS)
            .scalar()
        )

        refund_rows = q.filter(Transaction.type == TransactionType.refund)
        refund_count = refund_rows.count()
        refund_total = (
            self._db.query(func.coalesce(func.sum(Transaction.amount), 0))
            .filter(Transaction.created_at >= since, Transaction.type == TransactionType.refund)
            .scalar()
        )

        # service mix — share of successful transaction COUNT by type
        # (not naira volume; the dashboard renders this as a count-share bar).
        mix_rows = (
            self._db.query(Transaction.type, func.count(Transaction.id))
            .filter(Transaction.created_at >= since, Transaction.status == _SUCCESS)
            .group_by(Transaction.type)
            .all()
        )
        mix_total = sum(c for _, c in mix_rows) or 1
        service_mix = [
            {"type": t.value, "pct": round(c / mix_total, 4)} for t, c in mix_rows
        ]

        # daily success/failed transaction COUNTS for the dashboard chart
        # ("Transaction volume · Successful vs failed · daily").
        # NOTE: this pulls every in-window row into Python to bucket by day.
        # Fine at current volume; convert to a DB-side GROUP BY on
        # func.date(created_at) if a wide window ever becomes a hot path.
        daily: dict[str, dict[str, int]] = {}
        for tx in q.with_entities(Transaction.created_at, Transaction.status).all():
            day = tx.created_at.date().isoformat()
            bucket = daily.setdefault(day, {"success": 0, "failed": 0})
            if tx.status == _SUCCESS:
                bucket["success"] += 1
            elif tx.status == TransactionStatus.failed:
                bucket["failed"] += 1
        daily_volume = [
            {"date": d, "success": v["success"], "failed": v["failed"]}
            for d, v in sorted(daily.items())
        ]

        refunds_awaiting = (
            self._db.query(Transaction)
            .filter(Transaction.status.in_(_AWAITING))
            .count()
        )
        pending_over_5min = (
            self._db.query(Transaction)
            .filter(
                Transaction.status.in_(_PENDING),
                Transaction.created_at < now - timedelta(minutes=5),
            )
            .count()
        )

        return {
            "range_days": days,
            "volume_ngn": f"{Decimal(volume):.2f}",
            "transaction_count": total,
            "success_rate": success_rate,
            "refund_count": refund_count,
            "refund_total_ngn": f"{Decimal(refund_total):.2f}",
            "service_mix": service_mix,
            "daily_volume": daily_volume,
            "needs_attention": {
                "refunds_awaiting": refunds_awaiting,
                "transactions_pending_over_5min": pending_over_5min,
            },
        }

    def list_transactions(
        self, *, limit: int, offset: int, type_: str | None = None,
        status: str | None = None, date_from: datetime | None = None,
        date_to: datetime | None = None, user_id: str | None = None,
        q: str | None = None,
    ) -> dict:
        """Filterable, paginated transaction list for the ops dashboard.

        Joins User so each row carries the customer name without an N+1
        per-row lookup. Newest-first. ``total`` is the pre-pagination
        count so the dashboard can render page controls.
        """
        query = (
            self._db.query(Transaction, User)
            .join(User, User.id == Transaction.user_id)
        )
        if type_:
            query = query.filter(Transaction.type == TransactionType(type_))
        if status:
            query = query.filter(Transaction.status == TransactionStatus(status))
        if date_from:
            query = query.filter(Transaction.created_at >= date_from)
        if date_to:
            query = query.filter(Transaction.created_at <= date_to)
        if user_id:
            query = query.filter(Transaction.user_id == user_id)
        if q:
            like = f"%{q}%"
            query = query.filter(
                (Transaction.reference.ilike(like)) | (User.full_name.ilike(like))
            )
        total = query.count()
        rows = (
            query.order_by(Transaction.created_at.desc())
            .limit(limit).offset(offset).all()
        )
        items = [
            {
                "reference": tx.reference,
                "type": tx.type.value,
                "status": tx.status.value,
                "amount": f"{tx.amount:.2f}",
                "customer_name": user.full_name,
                "created_at": tx.created_at.isoformat(),
            }
            for tx, user in rows
        ]
        return {"items": items, "total": total, "limit": limit, "offset": offset}

    def get_transaction_detail(self, *, reference: str) -> dict | None:
        """Full investigation payload for one transaction.

        Returns None when the reference is unknown so the endpoint can map
        it to a 404. PII (email/phone) is returned in FULL — the admin
        surface is trusted and ops needs it to investigate.
        """
        tx = (
            self._db.query(Transaction)
            .filter(Transaction.reference == reference)
            .first()
        )
        if tx is None:
            return None
        user = self._db.query(User).filter(User.id == tx.user_id).first()
        events = (
            self._db.query(TransactionEvent)
            .filter(TransactionEvent.transaction_id == tx.id)
            .order_by(TransactionEvent.created_at.asc())
            .all()
        )
        payment = (
            self._db.query(Payment)
            .filter(Payment.transaction_id == tx.id)
            .first()
        )
        return {
            "reference": tx.reference,
            "type": tx.type.value,
            "status": tx.status.value,
            "amount": f"{tx.amount:.2f}",
            "fee": f"{tx.fee:.2f}",
            "meta": tx.meta,
            "created_at": tx.created_at.isoformat(),
            "user": None if user is None else {
                "id": str(user.id), "full_name": user.full_name,
                "email": user.email, "phone": user.phone,
                "kyc_tier": user.kyc_level.numeric,
            },
            "payment": None if payment is None else {
                "provider": payment.provider,
                "provider_reference": payment.provider_reference,
                "status": payment.status.value,
                "method": payment.method,
                "last4": payment.last4,
                "bank_name": payment.bank_name,
            },
            "events": [
                {
                    "from_status": e.from_status.value if e.from_status else None,
                    "to_status": e.to_status.value if e.to_status else None,
                    "reason": e.reason,
                    "context": e.context,
                    "created_at": e.created_at.isoformat(),
                }
                for e in events
            ],
        }
