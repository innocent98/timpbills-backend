"""Read-side queries for the admin dashboard. Endpoints stay thin; all
aggregation + listing logic lives here. Computes ONLY metrics backed by
real data — no avg-processing-time (not persisted), no flight metrics."""
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db.models._enums import TransactionStatus, TransactionType
from app.db.models.notification_log import (
    NotificationChannel,
    NotificationLog,
    NotificationLogStatus,
)
from app.db.models.payment import Payment
from app.db.models.transaction import Transaction
from app.db.models.transaction_event import TransactionEvent
from app.db.models.user import KycLevel, User
from app.db.models.wallet import Wallet

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
        self, *, limit: int, offset: int, type_: TransactionType | None = None,
        status: TransactionStatus | None = None, date_from: datetime | None = None,
        date_to: datetime | None = None, user_id: str | None = None,
        q: str | None = None,
    ) -> dict:
        """Filterable, paginated transaction list for the ops dashboard.

        Joins User so each row carries the customer name without an N+1
        per-row lookup. Newest-first. ``total`` is the pre-pagination
        count so the dashboard can render page controls.

        ``type_``/``status`` are already-parsed enum members (the endpoint
        validates the raw query strings into 400s); ``date_from`` is
        inclusive, ``date_to`` exclusive.
        """
        query = (
            self._db.query(Transaction, User)
            .join(User, User.id == Transaction.user_id)
        )
        if type_:
            query = query.filter(Transaction.type == type_)
        if status:
            query = query.filter(Transaction.status == status)
        if date_from:
            query = query.filter(Transaction.created_at >= date_from)
        if date_to:
            query = query.filter(Transaction.created_at < date_to)
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

    # UI status vocabulary → which refund-row DB statuses map onto it.
    # A refund Transaction is born with status=success (money credited) in
    # TransactionService.create_refund, so success → "processed". The
    # refund_failed/failed states (a refund that itself didn't land) →
    # "failed". Anything still in motion (pending/processing/refund_pending)
    # → "pending". The original tx's refund_pending→refunded walk lives on
    # the ORIGINAL row, not the refund row, so it's irrelevant here.
    _REFUND_STATUS_MAP: dict[str, tuple[TransactionStatus, ...]] = {
        "processed": (TransactionStatus.success, TransactionStatus.refunded),
        "failed": (TransactionStatus.failed, TransactionStatus.refund_failed),
        "pending": (
            TransactionStatus.pending,
            TransactionStatus.processing,
            TransactionStatus.refund_pending,
        ),
    }

    @classmethod
    def _ui_refund_status(cls, db_status: TransactionStatus) -> str:
        for ui, members in cls._REFUND_STATUS_MAP.items():
            if db_status in members:
                return ui
        return "pending"

    def list_refunds(
        self, *, limit: int, offset: int, status: str | None = None,
    ) -> dict:
        """Filterable, paginated refund list for the platform-admin refunds page.

        Refunds are ``Transaction`` rows with ``type=refund`` (separate rows,
        not a column/state on the original). Each links to its originating tx
        via ``meta["original_reference"]`` and carries ``meta["original_type"]``.

        The UI status (``pending``/``processed``/``failed``) is DERIVED from the
        refund row's own DB status (see ``_REFUND_STATUS_MAP``), so the optional
        ``status`` filter takes that UI vocabulary — the endpoint validates it
        into a 400 ``INVALID_FILTER`` before calling here; we map it to the set
        of underlying DB enum values for the WHERE clause.

        ``reason`` and the manual/auto signal live on the ``TransactionEvent``
        attached to the refund row (an admin-triggered refund's event reason is
        prefixed ``admin_manual_refund``). We batch-load those events for the
        page in one query to avoid an N+1.
        """
        query = (
            self._db.query(Transaction, User)
            .join(User, User.id == Transaction.user_id)
            .filter(Transaction.type == TransactionType.refund)
        )
        if status:
            query = query.filter(
                Transaction.status.in_(self._REFUND_STATUS_MAP[status])
            )
        total = query.count()
        rows = (
            query.order_by(Transaction.created_at.desc())
            .limit(limit).offset(offset).all()
        )

        # Batch-load the earliest event per refund for reason + manual signal.
        refund_ids = [tx.id for tx, _ in rows]
        events_by_tx: dict[object, TransactionEvent] = {}
        if refund_ids:
            for row_ev in (
                self._db.query(TransactionEvent)
                .filter(TransactionEvent.transaction_id.in_(refund_ids))
                .order_by(TransactionEvent.created_at.asc())
                .all()
            ):
                events_by_tx.setdefault(row_ev.transaction_id, row_ev)

        items = []
        for tx, user in rows:
            ev: TransactionEvent | None = events_by_tx.get(tx.id)
            reason = ev.reason if ev else None
            manual = bool(reason and reason.startswith("admin_manual_refund"))
            original_ref = (tx.meta or {}).get("original_reference")
            original_type = (tx.meta or {}).get("original_type")
            items.append({
                "reference": tx.reference,
                "original_reference": original_ref,
                "type": original_type or tx.type.value,
                "amount": f"{tx.amount:.2f}",
                "customer_name": user.full_name,
                "reason": reason,
                "status": self._ui_refund_status(tx.status),
                "created_at": tx.created_at.isoformat(),
                "manual": manual,
            })
        return {"items": items, "total": total, "limit": limit, "offset": offset}

    def list_notifications(
        self, *, limit: int, offset: int,
        channel: NotificationChannel | None = None,
        status: NotificationLogStatus | None = None,
        event: str | None = None, user_id: str | None = None,
    ) -> dict:
        """Filterable, paginated notification-log list for the admin dashboard.

        Read-only view over the delivery audit trail (``notification_logs``).
        Newest-first; ``total`` is the pre-pagination count for page controls.
        ``channel``/``status`` are already-parsed enum members (the endpoint
        validates the raw query strings into 400 ``INVALID_FILTER``), matching
        the ``list_transactions(type_, status)`` convention; ``event``/
        ``user_id`` are exact-match free-text filters.
        """
        query = self._db.query(NotificationLog)
        if channel is not None:
            query = query.filter(NotificationLog.channel == channel)
        if status is not None:
            query = query.filter(NotificationLog.status == status)
        if event:
            query = query.filter(NotificationLog.event == event)
        if user_id:
            query = query.filter(NotificationLog.user_id == user_id)
        total = query.count()
        rows = (
            query.order_by(NotificationLog.created_at.desc())
            .limit(limit).offset(offset).all()
        )
        items = [
            {
                "id": str(r.id),
                "user_id": str(r.user_id) if r.user_id else None,
                "event": r.event,
                "channel": r.channel.value,
                "status": r.status.value,
                "provider": r.provider,
                "provider_reference": r.provider_reference,
                "error": r.error,
                "created_at": r.created_at.isoformat(),
                "sent_at": r.sent_at.isoformat() if r.sent_at else None,
            }
            for r in rows
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

    def list_users(
        self, *, limit: int, offset: int, q: str | None = None,
        tier: KycLevel | None = None, status: str | None = None,
    ) -> dict:
        """Filterable, paginated user list for the admin dashboard.

        Outer-joins Wallet so each row carries the balance without an N+1
        per-row lookup (a user may have no wallet row yet). ``q`` matches
        name/email/phone (ILIKE). ``status`` is a free-text active/deleted
        filter, not an enum. Newest-first; ``total`` is the pre-pagination
        count. PII (email/phone) is returned in FULL — trusted surface.
        """
        query = self._db.query(User, Wallet).outerjoin(Wallet, Wallet.user_id == User.id)
        if q:
            like = f"%{q}%"
            query = query.filter(
                User.full_name.ilike(like) | User.email.ilike(like) | User.phone.ilike(like)
            )
        if tier is not None:
            query = query.filter(User.kyc_level == tier)
        if status == "active":
            query = query.filter(User.is_active.is_(True), User.deleted_at.is_(None))
        elif status == "deleted":
            query = query.filter(User.deleted_at.isnot(None))
        total = query.count()
        rows = query.order_by(User.created_at.desc()).limit(limit).offset(offset).all()
        items = [
            {
                "id": str(u.id),
                "full_name": u.full_name,
                "email": u.email,
                "phone": u.phone,
                "kyc_tier": u.kyc_level.numeric,
                "wallet_balance": f"{(w.balance if w else 0):.2f}",
                "status": "deleted" if u.deleted_at else ("active" if u.is_active else "disabled"),
                "created_at": u.created_at.isoformat(),
            }
            for u, w in rows
        ]
        return {"items": items, "total": total, "limit": limit, "offset": offset}

    def get_user_detail(self, *, user_id: str) -> dict | None:
        """Full profile payload for one user — profile + wallet + referral
        + the 10 most-recent transactions.

        Returns None when the id is unknown OR not a valid UUID so the
        endpoint maps both to a clean 404 (never a 500). PII is returned in
        FULL — admin is a trusted surface.
        """
        import uuid

        try:
            uid = uuid.UUID(user_id)
        except (TypeError, ValueError):
            return None
        u = self._db.query(User).filter(User.id == uid).first()
        if u is None:
            return None
        w = self._db.query(Wallet).filter(Wallet.user_id == uid).first()
        recent = (
            self._db.query(Transaction)
            .filter(Transaction.user_id == uid)
            .order_by(Transaction.created_at.desc())
            .limit(10).all()
        )
        referred_count = (
            self._db.query(User).filter(User.referred_by_user_id == uid).count()
        )
        return {
            "id": str(u.id),
            "full_name": u.full_name,
            "email": u.email,
            "phone": u.phone,
            "kyc_tier": u.kyc_level.numeric,
            "email_verified": u.email_verified,
            "phone_verified": u.is_phone_verified,
            "status": "deleted" if u.deleted_at else ("active" if u.is_active else "disabled"),
            "created_at": u.created_at.isoformat(),
            "wallet_balance": f"{(w.balance if w else 0):.2f}",
            "wallet_cap": f"{(w.balance_cap if w else 0):.2f}",
            "referral": {"code": u.referral_code, "referred_count": referred_count},
            "recent_transactions": [
                {
                    "reference": t.reference, "type": t.type.value,
                    "status": t.status.value, "amount": f"{t.amount:.2f}",
                    "created_at": t.created_at.isoformat(),
                }
                for t in recent
            ],
        }
