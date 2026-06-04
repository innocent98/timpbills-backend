"""NotificationService — fan-out for user-facing events.

Sprint 3 B13–B15 scope: email + push dispatch for bill_success,
bill_failure_refund, wallet_funded. On-screen (in-app feed) is a
future enhancement and requires a notifications table — out of scope
here; the email + push channels are what Nigerian users actually
read on their phones.

Why this is a service, not inline calls in webhooks/BillService:

 1. We want one place to decide "what does the user see for event X?"
    — copy, channel mix, suppression rules all belong together.
 2. Every dispatch goes through a Celery task (`notification_tasks.dispatch_delay`)
    so the request thread never blocks on SMTP / FCM latency. The
    service encapsulates that choice; callers just call `dispatch_delay`.
 3. A single entry point makes it easy to add channels or suppress an
    event globally (e.g. "disable bill_success emails for this user")
    without hunting through call sites.

Failures are swallowed with a warning log. A missing push token or a
transient email outage should never break the request that triggered
the notification — the tx is already committed, money already moved.
"""
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import TYPE_CHECKING, Any
from uuid import UUID

from app.core.logger import log
from app.db.models.notification_log import NotificationChannel, NotificationLog
from app.integrations.email.base import EmailProvider
from app.integrations.email.renderer import render_email
from app.integrations.push.base import BasePushClient

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from app.services.push_tokens_service import PushTokensService


# S3C-M11: uses the shared loguru-backed logger from app.core.logger
# so Sentry breadcrumbs are wired consistently with the rest of app/services.
# (No log = ... needed — `log` is imported above.)


class NotificationEvent(str, Enum):
    """Events the notification service knows how to dispatch. Keep
    short, snake_case, and stable — these strings land in logs and
    (eventually) in a notifications table."""

    bill_success                    = "bill_success"
    bill_failure_refund             = "bill_failure_refund"
    wallet_funded                   = "wallet_funded"
    # Electricity-specific success event: the generic "bill_success"
    # email says "X is on its way" and has nowhere to put the token.
    # This event renders token + units + DisCo + meter.
    electricity_token_delivered     = "electricity_token_delivered"
    # Cable-specific success event: the email shows the activated
    # bouquet (renew or change) and when it runs through. The generic
    # bill_success template has no slot for a plan name / validity.
    cable_activated                 = "cable_activated"
    # Reserved for a future flow (e.g. ops-initiated refunds, reconcile
    # worker refunds where we want a distinct user-visible message).
    refund_complete                 = "refund_complete"
    # Sprint 5b: referral-system pushes. No email templates today —
    # these are push-only events sent to the referrer at the milestone
    # moments (signup with their code, qualifying-tx credit issued).
    # The referee receives the welcome-bonus push at credit time.
    referrer_signup_notified        = "referrer_signup_notified"
    referral_credited               = "referral_credited"
    welcome_bonus                   = "welcome_bonus"


# ─── Notification categories (Sprint 5c · Task 5.2) ─────────────────────────
#
# Each NotificationEvent belongs to exactly one user-facing category. The
# NotificationPreference row carries one boolean per category; dispatch
# consults the user's row before fanning out to push. Categories are
# *push-side* — email is independently gated by `email_notifications`.
#
# Why an explicit category enum (instead of inlining strings): the
# NotificationPreference model already exposes these four columns. Mirroring
# them as an enum here makes the mapping side a compile-time invariant
# (mypy will complain if a column name drifts) and keeps the EVENT_CATEGORY
# table self-documenting.


class NotificationCategory(str, Enum):
    transaction_alerts = "transaction_alerts"
    referral_updates = "referral_updates"
    promotions = "promotions"


# Single source of truth for "which preference flag gates which event".
# A NotificationEvent missing from this map would bypass user preferences
# entirely — tests/services/test_notification_gating.py pins coverage so a
# new event without a category entry breaks loudly at CI.
EVENT_CATEGORY: dict[NotificationEvent, NotificationCategory] = {
    NotificationEvent.bill_success:                NotificationCategory.transaction_alerts,
    NotificationEvent.bill_failure_refund:         NotificationCategory.transaction_alerts,
    NotificationEvent.wallet_funded:               NotificationCategory.transaction_alerts,
    NotificationEvent.electricity_token_delivered: NotificationCategory.transaction_alerts,
    NotificationEvent.cable_activated:             NotificationCategory.transaction_alerts,
    NotificationEvent.refund_complete:             NotificationCategory.transaction_alerts,
    NotificationEvent.referrer_signup_notified:    NotificationCategory.referral_updates,
    NotificationEvent.referral_credited:           NotificationCategory.referral_updates,
    NotificationEvent.welcome_bonus:               NotificationCategory.referral_updates,
}


@dataclass(frozen=True, slots=True)
class _ResolvedPrefs:
    """Snapshot of a user's notification preferences as consulted by
    dispatch. Either pulled from the user's row, or — when no row exists
    yet (lazy-creation defaults) or the lookup fails — populated from the
    documented spec §3.2 defaults: transactional + referral + email ON,
    promotions OFF. Defaults are deliberately permissive on the
    transactional channel so absence of a row never silently drops a
    receipt."""

    transaction_alerts:  bool = True
    referral_updates:    bool = True
    promotions:          bool = False
    email_notifications: bool = True

    def push_allowed(self, category: NotificationCategory) -> bool:
        if category is NotificationCategory.transaction_alerts:
            return self.transaction_alerts
        if category is NotificationCategory.referral_updates:
            return self.referral_updates
        if category is NotificationCategory.promotions:
            return self.promotions
        return True  # pragma: no cover — exhaustive above

    @property
    def email_allowed(self) -> bool:
        return self.email_notifications


_DEFAULT_PREFS = _ResolvedPrefs()


def _resolve_prefs(db: "Session | None", user_id: str) -> _ResolvedPrefs:
    """Load the user's NotificationPreference row → _ResolvedPrefs.

    Fallback chain:
      * ``db is None``           → spec-default prefs (legacy callers).
      * ``user_id`` not parseable → spec-default prefs.
      * Row not found            → spec-default prefs (matches lazy-create
                                   semantics: a user who has never opened
                                   the screen behaves as the documented
                                   default).
      * Any other DB error       → spec-default prefs + warning log so
                                   we never silently suppress a receipt
                                   on a transient DB hiccup.
    """
    if db is None:
        return _DEFAULT_PREFS
    try:
        user_uuid = UUID(user_id)
    except (TypeError, ValueError):
        return _DEFAULT_PREFS
    # Local import to avoid pulling the SQLAlchemy model graph at module
    # import time (the Celery worker boots this module on every task).
    from app.db.models.notification_preference import (  # noqa: PLC0415
        NotificationPreference,
    )
    try:
        row = (
            db.query(NotificationPreference)
            .filter(NotificationPreference.user_id == user_uuid)
            .first()
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("notify: prefs lookup failed user=%s err=%s", user_id, exc)
        return _DEFAULT_PREFS
    if row is None:
        return _DEFAULT_PREFS
    return _ResolvedPrefs(
        transaction_alerts=bool(row.transaction_alerts),
        referral_updates=bool(row.referral_updates),
        promotions=bool(row.promotions),
        email_notifications=bool(row.email_notifications),
    )


# Maps event → (email_template_name, push_title_template, push_body_key)
# None means "no email for this event" / "no push for this event".
_EMAIL_TEMPLATES: dict[NotificationEvent, str | None] = {
    NotificationEvent.bill_success:                "bill_success",
    NotificationEvent.bill_failure_refund:         "bill_failure_refund",
    NotificationEvent.wallet_funded:               "wallet_funded",
    NotificationEvent.electricity_token_delivered: "electricity_token_delivered",
    NotificationEvent.cable_activated:             "cable_activated",
    NotificationEvent.refund_complete:             None,
    NotificationEvent.referrer_signup_notified:    None,
    NotificationEvent.referral_credited:           None,
    NotificationEvent.welcome_bonus:               None,
}


@dataclass(frozen=True, slots=True)
class _PushCopy:
    title: str
    body: str


def _push_copy(event: NotificationEvent, ctx: dict[str, Any]) -> _PushCopy | None:
    """Short push copy per event. Kept compact because push notifications
    are truncated aggressively by iOS/Android on the lock screen."""
    if event is NotificationEvent.bill_success:
        if ctx.get("partial"):
            return _PushCopy(
                title="Delivered (partial)",
                body=f"₦{ctx['delivered_amount']} of {ctx['tx_type_label']} reached "
                     f"{ctx['destination']}. Shortfall refunded.",
            )
        return _PushCopy(
            title=f"{ctx['tx_type_label'].title()} delivered",
            body=f"₦{ctx['amount']} of {ctx['tx_type_label']} is on its way "
                 f"to {ctx['destination']}.",
        )
    if event is NotificationEvent.bill_failure_refund:
        return _PushCopy(
            title=f"{ctx['tx_type_label'].title()} refund",
            body=f"Your ₦{ctx['amount']} is back in your wallet.",
        )
    if event is NotificationEvent.wallet_funded:
        return _PushCopy(
            title="Wallet funded",
            body=f"₦{ctx['amount']} in. New balance: ₦{ctx['balance']}.",
        )
    if event is NotificationEvent.electricity_token_delivered:
        # Last four of the meter for context without leaking the full
        # identifier on a lock screen; body carries the token + kWh
        # since that's the whole reason the user opened the app.
        meter = str(ctx.get("meter_number", ""))
        meter_tail = meter[-4:] if len(meter) >= 4 else meter
        units_phrase = f" · {ctx['units']} kWh" if ctx.get("units") else ""
        return _PushCopy(
            title=f"Electricity purchased · Meter {meter_tail}",
            body=f"Token: {ctx.get('token', '')}{units_phrase}",
        )
    if event is NotificationEvent.cable_activated:
        mode_label = "renewed" if ctx.get("mode") == "renew" else "activated"
        return _PushCopy(
            title=f"{ctx.get('provider_label', 'Cable')} {mode_label}",
            body=f"{ctx.get('plan_name', '')} on your smartcard. ₦{ctx.get('amount', '')} paid.",
        )
    if event is NotificationEvent.referrer_signup_notified:
        name = ctx.get("referee_display_name") or "Someone"
        return _PushCopy(
            title="A friend just joined",
            body=f"{name} signed up with your referral code.",
        )
    if event is NotificationEvent.referral_credited:
        amount = ctx.get("amount_naira", "")
        return _PushCopy(
            title="Referral reward credited",
            body=f"₦{amount} added to your wallet for a successful referral.",
        )
    if event is NotificationEvent.welcome_bonus:
        amount = ctx.get("amount_naira", "")
        return _PushCopy(
            title="Welcome bonus added",
            body=f"₦{amount} landed in your wallet — enjoy.",
        )
    return None


class NotificationService:
    def __init__(
        self,
        *,
        email_client: EmailProvider,
        push_client: BasePushClient,
        push_tokens_service: "PushTokensService | None" = None,
        db: "Session | None" = None,
    ) -> None:
        """If ``push_tokens_service`` is wired, ``_maybe_push`` runs in
        token-aware mode: look up all registered FCM tokens for the
        user, send one push per device, evict dead tokens reported by
        the FCM client. If omitted (Sprint 3 default + tests that
        predate push-tokens storage), falls back to a single
        ``push_client.send(user_id=..., fcm_token=None)`` call — the
        FakePushClient is happy with that.

        Sprint 5c · Task 5.2: ``db`` enables NotificationPreference-aware
        gating. When wired, dispatch consults the user's preference row
        (or spec defaults if no row exists) before each channel; when
        ``None``, every event fires unconditionally. The Celery worker
        always wires it; legacy unit tests construct without it and
        retain the old default-on behaviour."""
        self._email = email_client
        self._push = push_client
        self._push_tokens = push_tokens_service
        self._db = db

    async def dispatch(
        self,
        *,
        user_id: str,
        user_email: str,
        event: NotificationEvent,
        context: dict[str, Any],
    ) -> None:
        """Send the email + push for this event. Each channel failure
        is caught and logged so neither blocks the other.

        Per-event gating: ``EVENT_CATEGORY`` maps the event onto one of
        the NotificationPreference push categories, then the user's row
        is consulted. ``email_notifications`` independently gates the
        email channel. Both lookups go through ``_resolve_prefs`` which
        falls back to spec defaults on any miss/error so a transient DB
        blip never silently drops a transactional push."""
        prefs = _resolve_prefs(self._db, user_id)

        if prefs.email_allowed:
            await self._maybe_email(
                user_id=user_id, user_email=user_email, event=event, context=context,
            )

        category = EVENT_CATEGORY.get(event)
        # An event without a category mapping is a bug (the dedicated
        # test_event_category_map_covers_every_event test catches this
        # at CI). Be defensive at runtime: if it ever happens in prod,
        # treat the event as transactional so the user still gets the
        # message — silent suppression would be the worse failure mode.
        push_allowed = (
            prefs.push_allowed(category) if category is not None else True
        )
        if push_allowed:
            await self._maybe_push(
                user_id=user_id, event=event, context=context,
            )

    # ── Best-effort audit logging (Task 13) ──────────────────────────────
    #
    # Every channel send records a notification_logs row: pending → sent /
    # failed. The writes are wrapped so a logging failure can NEVER break
    # the underlying send — an audit-trail hiccup must not cost a user their
    # receipt or token. When ``self._db`` is None (legacy unit tests that
    # construct the service without a session) logging is skipped entirely.

    def _log_pending(
        self,
        *,
        user_id: str | None,
        event: NotificationEvent,
        channel: NotificationChannel,
        provider: str,
    ) -> NotificationLog | None:
        if self._db is None:
            return None
        try:
            uid: UUID | None = None
            if user_id:
                try:
                    uid = UUID(user_id)
                except (TypeError, ValueError):
                    uid = None
            from app.services.notification_log_service import (  # noqa: PLC0415
                NotificationLogService,
            )
            return NotificationLogService(db=self._db).record_pending(
                user_id=uid, event=event.value, channel=channel, provider=provider,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "notify: log record_pending failed event=%s channel=%s err=%s",
                event.value, channel.value, exc,
            )
            return None

    def _log_sent(self, row: NotificationLog | None) -> None:
        if self._db is None or row is None:
            return
        try:
            from app.services.notification_log_service import (  # noqa: PLC0415
                NotificationLogService,
            )
            NotificationLogService(db=self._db).mark_sent(row)
        except Exception as exc:  # noqa: BLE001
            log.warning("notify: log mark_sent failed err=%s", exc)

    def _log_failed(self, row: NotificationLog | None, *, error: str) -> None:
        if self._db is None or row is None:
            return
        try:
            from app.services.notification_log_service import (  # noqa: PLC0415
                NotificationLogService,
            )
            NotificationLogService(db=self._db).mark_failed(row, error=error)
        except Exception as exc:  # noqa: BLE001
            log.warning("notify: log mark_failed failed err=%s", exc)

    async def _maybe_email(
        self,
        *,
        user_id: str,
        user_email: str,
        event: NotificationEvent,
        context: dict[str, Any],
    ) -> None:
        if not user_email:
            return
        tpl = _EMAIL_TEMPLATES.get(event)
        if tpl is None:
            return
        try:
            html, text = render_email(tpl, context)
        except Exception as exc:
            log.warning(
                "notify: render failed event=%s tpl=%s err=%s",
                event.value, tpl, exc,
            )
            return
        subject = _email_subject(event, context)
        log_row = self._log_pending(
            user_id=user_id, event=event,
            channel=NotificationChannel.email, provider="resend",
        )
        try:
            await self._email.send_text(
                to=user_email, subject=subject, html=html, text=text,
            )
        except Exception as exc:
            self._log_failed(log_row, error=str(exc))
            log.warning(
                "notify: email send failed event=%s to=%s err=%s",
                event.value, user_email, exc,
            )
        else:
            self._log_sent(log_row)

    async def _maybe_push(
        self, *, user_id: str, event: NotificationEvent, context: dict[str, Any]
    ) -> None:
        copy = _push_copy(event, context)
        if copy is None:
            return
        data = {
            "event":     event.value,
            "reference": str(context.get("reference", "")),
        }

        # One push audit row per dispatch (not per device): pending before
        # the send(s), sent if ≥1 device succeeds, failed if every device
        # fails (or the legacy single send raises).
        log_row = self._log_pending(
            user_id=user_id, event=event,
            channel=NotificationChannel.push, provider="fcm",
        )

        if self._push_tokens is None:
            # Legacy mode (Sprint 3 default + tests that predate
            # push-tokens storage) — one call per user, no fcm_token.
            try:
                await self._push.send(
                    user_id=user_id, title=copy.title, body=copy.body, data=data,
                )
            except Exception as exc:
                self._log_failed(log_row, error=str(exc))
                log.warning(
                    "notify: push send failed event=%s user=%s err=%s",
                    event.value, user_id, exc,
                )
            else:
                self._log_sent(log_row)
            return

        # Token-aware mode — real FCM. Fan out one send per registered
        # device and evict registrations that FCM reports as dead.
        try:
            tokens = self._push_tokens.list_for_user(user_id=UUID(user_id))
        except Exception as exc:
            self._log_failed(log_row, error=str(exc))
            log.warning(
                "notify: push device lookup failed event=%s user=%s err=%s",
                event.value, user_id, exc,
            )
            return
        if not tokens:
            # No devices to send to — nothing was delivered. Record the
            # dispatch as failed so the audit trail reflects that the push
            # never reached the user (rather than leaving a dangling pending).
            self._log_failed(log_row, error="no registered devices")
            return

        # Imported lazily so the legacy Sprint 3 tests (which never
        # exercise FCM) don't pay the google-auth import cost.
        from app.integrations.push.fcm import DeadFCMToken  # noqa: PLC0415

        any_sent = False
        last_error: str | None = None
        for row in tokens:
            try:
                await self._push.send(
                    user_id=user_id,
                    fcm_token=row.fcm_token,
                    title=copy.title, body=copy.body, data=data,
                )
            except DeadFCMToken as exc:
                last_error = str(exc) or "dead fcm token"
                try:
                    self._push_tokens.delete_by_fcm_token(fcm_token=row.fcm_token)
                except Exception as exc:
                    log.warning(
                        "notify: dead-device eviction failed user=%s err=%s",
                        user_id, exc,
                    )
            except Exception as exc:
                last_error = str(exc)
                log.warning(
                    "notify: push send failed event=%s user=%s err=%s",
                    event.value, user_id, exc,
                )
            else:
                any_sent = True

        if any_sent:
            self._log_sent(log_row)
        else:
            self._log_failed(log_row, error=last_error or "all device sends failed")


def _email_subject(event: NotificationEvent, ctx: dict[str, Any]) -> str:
    if event is NotificationEvent.bill_success:
        if ctx.get("partial"):
            return f"Partial delivery — ₦{ctx.get('delivered_amount')} sent"
        return f"Your ₦{ctx.get('amount')} {ctx.get('tx_type_label')} is on its way"
    if event is NotificationEvent.bill_failure_refund:
        return f"Refund: ₦{ctx.get('amount')} back in your wallet"
    if event is NotificationEvent.wallet_funded:
        return f"Wallet funded — ₦{ctx.get('amount')}"
    if event is NotificationEvent.electricity_token_delivered:
        return f"Electricity token — ₦{ctx.get('amount')} on meter {ctx.get('meter_number')}"
    if event is NotificationEvent.cable_activated:
        verb = "renewed" if ctx.get("mode") == "renew" else "activated"
        return f"{ctx.get('provider_label', 'Cable')} {verb} — {ctx.get('plan_name', '')}"
    return "Timpbills notification"


# ─── Convenience context builders ───────────────────────────────────────

def build_bill_context(
    *,
    tx_type: str,
    amount: Decimal,
    destination: str,
    reference: str,
    when: str,
    partial: bool = False,
    delivered_amount: Decimal | None = None,
    shortfall: Decimal | None = None,
) -> dict[str, Any]:
    """Shape the context dict the bill_success / bill_failure_refund
    templates expect. Keeping this in one place means template additions
    don't require visiting every call site."""
    ctx: dict[str, Any] = {
        "tx_type_label": _TX_TYPE_LABELS.get(tx_type, tx_type),
        "amount":        str(amount),
        "destination":   destination,
        "reference":     reference,
        "when":          when,
        "partial":       partial,
    }
    if partial:
        ctx["delivered_amount"] = str(delivered_amount or amount)
        ctx["shortfall"]        = str(shortfall or Decimal("0.00"))
    return ctx


def build_wallet_funded_context(
    *, amount: Decimal, balance: Decimal, reference: str, channel: str | None = None,
) -> dict[str, Any]:
    return {
        "amount":    str(amount),
        "balance":   str(balance),
        "reference": reference,
        "channel":   channel,
    }


def build_electricity_token_context(
    *,
    token: str,
    units: str | None,
    service_id: str,
    meter_number: str,
    amount: Decimal,
    reference: str,
    when: str,
    disco_label: str | None = None,
) -> dict[str, Any]:
    """Shape the context dict the electricity_token_delivered template
    expects. `disco_label` is a display label (e.g. "Ikeja Electric")
    resolved by the caller — we fall back to service_id if None so the
    email still renders a sensible value if the caller forgets."""
    return {
        "token":        token,
        "units":        units or "",
        "service_id":   service_id,
        "disco_label":  disco_label or service_id,
        "meter_number": meter_number,
        "amount":       str(amount),
        "reference":    reference,
        "when":         when,
    }


def build_cable_activated_context(
    *,
    service_id: str,
    smartcard_number: str,
    mode: str,                   # "renew" | "change"
    plan_code: str,
    plan_name: str,
    amount: Decimal,
    reference: str,
    when: str,
    provider_label: str | None = None,
) -> dict[str, Any]:
    """Shape the context dict for cable_activated. `provider_label` is
    the display form (e.g. "DStv"); falls back to service_id if None.
    `mode` drives whether the copy reads "renewed" or "activated"."""
    return {
        "service_id":       service_id,
        "provider_label":   provider_label or service_id.upper(),
        "smartcard_number": smartcard_number,
        "mode":             mode,
        "plan_code":        plan_code,
        "plan_name":        plan_name,
        "amount":           str(amount),
        "reference":        reference,
        "when":             when,
    }


_TX_TYPE_LABELS = {
    "airtime":     "airtime",
    "data":        "data",
    "electricity": "electricity token",
    "cable":       "cable TV",
    "flight":      "flight",
}
