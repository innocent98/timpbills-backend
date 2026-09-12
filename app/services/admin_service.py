"""Read-side queries for the admin dashboard. Endpoints stay thin; all
aggregation + listing logic lives here. Computes ONLY metrics backed by
real data — no avg-processing-time (not persisted), no flight metrics."""
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import func, select
from sqlalchemy.orm import Session

if TYPE_CHECKING:
    from app.db.models.admin_user import AdminUser
    from app.integrations.paystack.base import PaymentProvider
    from app.integrations.vtpass.base import BillProvider
    from app.services.token_store import TokenStore

from app.core.logger import log
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
from app.utils.email import normalize_email
from app.utils.phone import InvalidPhoneFormat, normalize_to_e164

_SUCCESS = TransactionStatus.success
_AWAITING = (TransactionStatus.refund_pending, TransactionStatus.refund_failed)
_PENDING = (TransactionStatus.pending, TransactionStatus.processing)


class AdminService:
    def __init__(self, *, db: Session) -> None:
        self._db = db

    def _window_metrics(self, start: datetime, end: datetime) -> dict:
        """Aggregate count/volume/refund metrics for a half-open [start, end) window."""
        base = self._db.query(Transaction).filter(
            Transaction.created_at >= start, Transaction.created_at < end
        )
        total = base.count()
        success_count = base.filter(Transaction.status == _SUCCESS).count()
        volume = (
            self._db.query(func.coalesce(func.sum(Transaction.amount), 0))
            .filter(
                Transaction.created_at >= start, Transaction.created_at < end,
                Transaction.status == _SUCCESS,
            )
            .scalar()
        )
        refund_q = base.filter(Transaction.type == TransactionType.refund)
        refund_count = refund_q.count()
        refund_total = (
            self._db.query(func.coalesce(func.sum(Transaction.amount), 0))
            .filter(
                Transaction.created_at >= start, Transaction.created_at < end,
                Transaction.type == TransactionType.refund,
            )
            .scalar()
        )
        return {
            "transaction_count": total,
            "success_count": success_count,
            "volume": Decimal(volume),
            "refund_total": Decimal(refund_total),
            "refund_count": refund_count,
        }

    @staticmethod
    def _rel_delta(cur: Decimal, prior: Decimal) -> float | None:
        """Relative change (cur-prior)/prior as a float, or None when prior is 0."""
        if prior == 0:
            return None
        return float((cur - prior) / prior)

    def overview(self, *, days: int = 7) -> dict:
        now = datetime.now(UTC)
        since = now - timedelta(days=days)

        cur = self._window_metrics(since, now)
        prior = self._window_metrics(since - timedelta(days=days), since)

        total = cur["transaction_count"]
        success_rate = round(cur["success_count"] / total, 4) if total else 0.0
        volume = cur["volume"]
        refund_count = cur["refund_count"]
        refund_total = cur["refund_total"]

        prior_rate = (
            prior["success_count"] / prior["transaction_count"]
            if prior["transaction_count"]
            else None
        )
        deltas = {
            "success_rate_pp": (
                round((success_rate - prior_rate) * 100, 1)
                if prior_rate is not None
                else None
            ),
            "volume_pct": self._rel_delta(cur["volume"], prior["volume"]),
            "refund_total_pct": self._rel_delta(cur["refund_total"], prior["refund_total"]),
        }

        # service mix + daily_volume still scan the current window directly.
        q = self._db.query(Transaction).filter(Transaction.created_at >= since)

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
            "deltas": deltas,
            "service_mix": service_mix,
            "daily_volume": daily_volume,
            "needs_attention": {
                "refunds_awaiting": refunds_awaiting,
                "transactions_pending_over_5min": pending_over_5min,
            },
        }

    def list_transactions(
        self, *, limit: int, offset: int, type_: TransactionType | None = None,
        exclude_type: TransactionType | None = None,
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
        if exclude_type:
            query = query.filter(Transaction.type != exclude_type)
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
        wallet = (
            self._db.query(Wallet)
            .filter(Wallet.user_id == tx.user_id)
            .first()
        )
        refund_row = None
        if tx.type != TransactionType.refund:
            refund_candidates = (
                self._db.query(Transaction)
                .filter(
                    Transaction.user_id == tx.user_id,
                    Transaction.type == TransactionType.refund,
                )
                .all()
            )
            refund_row = next(
                (c for c in refund_candidates
                 if c.meta and c.meta.get("original_reference") == tx.reference),
                None,
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
                "wallet_balance": f"{(wallet.balance if wallet else 0):.2f}",
                "created_at": user.created_at.isoformat(),
            },
            "payment": None if payment is None else {
                "provider": payment.provider,
                "provider_reference": payment.provider_reference,
                "status": payment.status.value,
                "method": payment.method,
                "last4": payment.last4,
                "bank_name": payment.bank_name,
            },
            "refund": None if refund_row is None else {
                "reference": refund_row.reference,
                "amount": f"{refund_row.amount:.2f}",
                "status": refund_row.status.value,
                "created_at": refund_row.created_at.isoformat(),
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
        elif status == "disabled":
            query = query.filter(User.is_active.is_(False), User.deleted_at.is_(None))
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

    async def update_user(
        self,
        *,
        user_id: str,
        patch: dict,
        actor: "AdminUser",
        token_store: "TokenStore",
    ) -> dict:
        """Edit a user's basic identity fields (name / email / phone).

        Trusted admin surface, so email/phone changes apply WITHOUT the OTP
        step the self-service flows require. Side effects mirror the user's
        own verified-change paths:

          * ``email`` change -> ``email_verified = False`` (must re-verify).
          * ``phone`` change -> ``is_phone_verified = False`` + stamp
            ``tokens_revoked_at`` + ``token_store.revoke_all`` so every
            outstanding access/refresh token for that user dies. The user is
            effectively signed out (mirrors ``confirm_phone_change``).

        Raises ``ValueError`` with a stable code the endpoint maps to HTTP:
          NO_FIELDS -> 400, USER_NOT_FOUND -> 404,
          EMAIL_ALREADY_IN_USE / PHONE_ALREADY_IN_USE -> 409,
          INVALID_PHONE -> 422.

        Returns the fresh ``get_user_detail`` dict (same shape as
        ``GET /admin/users/{id}``) so the FE can replace its state directly.

        Audit: emits one structured ``log.info`` line with the actor id/email,
        the target id, and the LIST OF CHANGED FIELD NAMES only — never the
        new email/phone VALUES (PII stays out of the ops log stream).
        """
        import uuid

        _editable = ("full_name", "email", "phone")
        if not any(patch.get(f) is not None for f in _editable):
            raise ValueError("NO_FIELDS")

        try:
            uid = uuid.UUID(user_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("USER_NOT_FOUND") from exc
        user = self._db.query(User).filter(User.id == uid).first()
        if user is None:
            raise ValueError("USER_NOT_FOUND")

        changed: list[str] = []
        revoke_tokens = False

        if patch.get("full_name") is not None:
            new_name = patch["full_name"].strip()
            if new_name != user.full_name:
                user.full_name = new_name
                changed.append("full_name")

        if patch.get("email") is not None:
            new_email = normalize_email(patch["email"])
            if new_email != user.email:
                collision = (
                    self._db.query(User)
                    .filter(User.email == new_email, User.id != user.id)
                    .first()
                )
                if collision is not None:
                    raise ValueError("EMAIL_ALREADY_IN_USE")
                user.email = new_email
                user.email_verified = False
                changed.append("email")

        if patch.get("phone") is not None:
            try:
                new_phone = normalize_to_e164(patch["phone"])
            except InvalidPhoneFormat as exc:
                raise ValueError("INVALID_PHONE") from exc
            if new_phone != user.phone:
                collision = (
                    self._db.query(User)
                    .filter(User.phone == new_phone, User.id != user.id)
                    .first()
                )
                if collision is not None:
                    raise ValueError("PHONE_ALREADY_IN_USE")
                user.phone = new_phone
                user.is_phone_verified = False
                # Rotating the phone invalidates every live session: stamp the
                # access-token gate and nuke the refresh keyspace (below, after
                # commit) exactly as confirm_phone_change does.
                user.tokens_revoked_at = datetime.now(UTC)
                revoke_tokens = True
                changed.append("phone")

        self._db.commit()

        # Refresh-token revocation is a Redis side effect — run it AFTER the
        # DB commit so a rollback never leaves tokens killed for an un-applied
        # change (same ordering as AuthService.confirm_phone_change).
        if revoke_tokens:
            await token_store.revoke_all(user_id=str(user.id))

        # Audit: field NAMES only, never the new email/phone values (PII).
        log.info(
            "admin_user_update actor_id=%s actor_email=%s target_user_id=%s changed_fields=%s",
            actor.id,
            actor.email,
            str(user.id),
            changed,
        )

        return self.get_user_detail(user_id=str(user.id))

    async def requery_transaction(
        self,
        *,
        reference: str,
        actor_admin_id,
        bill_provider: "BillProvider",
        payment_provider: "PaymentProvider",
    ) -> dict | None:
        """On-demand provider re-poll for a STUCK transaction — the ops
        counterpart of the periodic reconcile sweep.

        Returns a small dict the endpoint serializes:
        ``{"reference", "status", "requeried"}``; ``None`` when the
        reference is unknown (endpoint maps to 404).

        Behaviour mirrors the reconcile tasks EXACTLY:

          * Terminal tx (success/failed/refund_*) → no-op, ``requeried=False``,
            provider NOT called. (Same final-state set the reconcile sweeps
            skip — there's nothing to re-poll.)
          * Bill tx (airtime/data/electricity/cable/flight) → ``BillProvider.
            requery(request_id=reference)`` then re-lock the row and apply via
            ``BillService.apply_provider_result`` — the same call
            ``_reconcile_bills`` makes. delivered→success(+partial refund),
            failed→refund+credit+failed, pending→unchanged.
          * wallet_funding tx → Paystack ``verify`` on the linked Payment +
            ``_claim_payment``-style lock + wallet credit + transition, exactly
            as ``_reconcile``. No refund on failure: a declined card never took
            money (wallet_funding is intentionally OUT of REFUNDABLE_ON_FAILURE).

        We deliberately OMIT the batch-only bookkeeping (defer counters,
        permanent-attempts escalation, the abandoned→leave-pending sweep
        cadence): those only make sense across a periodic loop. The state
        transitions and refund path are identical to the sweep.

        Every call writes one audit ``TransactionEvent`` (``reason=
        "admin_requery"``, ``context={"actor_admin_user_id": ...}``) — even
        the terminal no-op and the still-pending paths — so ops can see
        "an admin re-polled this and here's what happened".
        """
        # Lazy imports: keep the admin_service import graph lean (these pull
        # the bill/wallet/tx service stack + redis types) and avoid a circular
        # import with app.api.deps (which imports AdminService transitively).
        from app.api.deps import get_redis
        from app.db.models._enums import TransactionType as _TxType
        from app.db.models.payment import PaymentStatus as _PayStatus
        from app.services.bill_service import BillService
        from app.services.transaction_service import TransactionService
        from app.services.wallet_service import WalletService

        tx = (
            self._db.query(Transaction)
            .filter(Transaction.reference == reference)
            .first()
        )
        if tx is None:
            return None

        actor_ctx = {"actor_admin_user_id": str(actor_admin_id)}

        # Terminal states — nothing to re-poll. No-op (matches the
        # _TX_FINAL_STATES skip in both reconcile tasks). We still audit.
        if tx.status not in _PENDING:
            self._write_audit(tx, reason="admin_requery", context={
                **actor_ctx, "requeried": False, "status": tx.status.value,
            })
            self._db.commit()
            return {
                "reference": tx.reference,
                "status": tx.status.value,
                "requeried": False,
            }

        tx_svc = TransactionService(db=self._db)
        wallet_svc = WalletService(db=self._db)

        if tx.type == _TxType.wallet_funding:
            new_status = await self._requery_funding(
                tx=tx, payment_provider=payment_provider,
                tx_svc=tx_svc, wallet_svc=wallet_svc, pay_status=_PayStatus,
            )
        else:
            bill_svc = BillService(
                db=self._db, tx_svc=tx_svc, wallet_svc=wallet_svc,
                provider=bill_provider, redis=get_redis(),
            )
            new_status = await self._requery_bill(
                tx=tx, bill_provider=bill_provider, bill_svc=bill_svc,
            )

        self._write_audit(tx, reason="admin_requery", context={
            **actor_ctx, "requeried": True, "status": new_status,
        })
        self._db.commit()
        return {
            "reference": tx.reference,
            "status": new_status,
            "requeried": True,
        }

    async def _requery_bill(
        self, *, tx: Transaction, bill_provider, bill_svc
    ) -> str:
        """Mirror of _reconcile_bills for a single tx: requery → re-lock →
        skip-if-terminal → apply_provider_result. Returns the resulting
        status string."""
        from app.services.bill_service import _TX_FINAL_STATES

        result = await bill_provider.requery(request_id=tx.reference)

        # Re-fetch under a row-lock. A webhook / sweep may have finalized the
        # tx between our SELECT and now — applying blind would race them or
        # raise InvalidStateTransition. Same guard the sweep uses.
        self._db.expire(tx)
        locked = self._db.execute(
            select(Transaction)
            .where(Transaction.id == tx.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).scalar_one()
        if locked.status in _TX_FINAL_STATES:
            return locked.status.value

        bill_svc.apply_provider_result(
            tx=locked, amount=locked.amount, result=result
        )
        return locked.status.value

    async def _requery_funding(
        self, *, tx: Transaction, payment_provider, tx_svc, wallet_svc, pay_status
    ) -> str:
        """Mirror of _reconcile for a single wallet_funding tx: verify the
        linked Payment, claim it pending→success/failed, credit + transition
        on success, transition-failed (no refund) on failure. Returns the
        resulting status string."""
        payment = (
            self._db.query(Payment)
            .filter(Payment.transaction_id == tx.id)
            .first()
        )
        if payment is None:
            # No payment row to verify against — leave the tx untouched.
            return tx.status.value

        v = await payment_provider.verify(reference=payment.provider_reference)

        from app.services.wallet_service import (
            InsufficientBalance,
            KycCapExceeded,
        )

        if v.status == "success":
            if not self._claim_payment(payment.id, pay_status.success, pay_status):
                return tx.status.value
            if v.authorization is not None:
                payment.method = v.authorization.channel
                payment.last4 = v.authorization.last4
                payment.bank_name = v.authorization.bank
            try:
                wallet_svc.credit(user_id=tx.user_id, amount=tx.amount)
            except (KycCapExceeded, InsufficientBalance):
                # Domain exception on an otherwise-valid funding — defer,
                # exactly as _reconcile does. The credit raised BEFORE its
                # internal commit, so the uncommitted Payment claim is
                # reverted by this rollback; Payment stays pending for a
                # later retry (ops may raise the user's cap meanwhile). We
                # do NOT credit, do NOT transition, do NOT 500 — return the
                # tx's current (unchanged) status as "could not resolve now".
                self._db.rollback()
                return tx.status.value
            tx_svc.transition(
                tx, to_status=TransactionStatus.success,
                reason="admin_requery.verify.success",
            )
        elif v.status == "failed":
            if not self._claim_payment(payment.id, pay_status.failed, pay_status):
                return tx.status.value
            tx_svc.transition(
                tx, to_status=TransactionStatus.failed,
                reason="admin_requery.verify.failed",
            )
            # No refund: wallet_funding is intentionally OUT of
            # REFUNDABLE_ON_FAILURE — a declined card never took money.
        # abandoned / unknown → leave pending, no transition.

        self._db.refresh(tx)
        return tx.status.value

    def _claim_payment(self, payment_id, target_status, pay_status) -> bool:
        """Atomically flip Payment.status pending → target under a row-lock.
        Returns True if this call won the race (mirrors reconcile's
        _claim_payment); a no-op if a concurrent webhook already mutated it."""
        locked = (
            self._db.query(Payment)
            .filter(Payment.id == payment_id)
            .with_for_update()
            .one()
        )
        if locked.status != pay_status.pending:
            return False
        locked.status = target_status
        return True

    def _write_audit(self, tx: Transaction, *, reason: str, context: dict) -> None:
        """Audit row on the tx. from_status==to_status (placeholder): the
        real state transitions write their own events via TransactionService;
        this row records that an admin re-polled and the outcome."""
        self._db.add(TransactionEvent(
            transaction_id=tx.id,
            from_status=tx.status,
            to_status=tx.status,
            reason=reason,
            context=context,
        ))
