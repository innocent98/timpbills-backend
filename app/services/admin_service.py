"""Read-side queries for the admin dashboard. Endpoints stay thin; all
aggregation + listing logic lives here. Computes ONLY metrics backed by
real data — no avg-processing-time (not persisted), no flight metrics."""
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db.models._enums import TransactionStatus, TransactionType
from app.db.models.transaction import Transaction

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
