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
from typing import Any

from app.core.logger import log
from app.integrations.email.base import EmailProvider
from app.integrations.email.renderer import render_email
from app.integrations.push.base import BasePushClient


# S3C-M11: uses the shared loguru-backed logger from app.core.logger
# so Sentry breadcrumbs are wired consistently with the rest of app/services.
# (No log = ... needed — `log` is imported above.)


class NotificationEvent(str, Enum):
    """Events the notification service knows how to dispatch. Keep
    short, snake_case, and stable — these strings land in logs and
    (eventually) in a notifications table."""

    bill_success         = "bill_success"
    bill_failure_refund  = "bill_failure_refund"
    wallet_funded        = "wallet_funded"
    # Reserved for a future flow (e.g. ops-initiated refunds, reconcile
    # worker refunds where we want a distinct user-visible message).
    refund_complete      = "refund_complete"


# Maps event → (email_template_name, push_title_template, push_body_key)
# None means "no email for this event" / "no push for this event".
_EMAIL_TEMPLATES: dict[NotificationEvent, str | None] = {
    NotificationEvent.bill_success:        "bill_success",
    NotificationEvent.bill_failure_refund: "bill_failure_refund",
    NotificationEvent.wallet_funded:       "wallet_funded",
    NotificationEvent.refund_complete:     None,
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
    return None


class NotificationService:
    def __init__(
        self,
        *,
        email_client: EmailProvider,
        push_client: BasePushClient,
    ) -> None:
        self._email = email_client
        self._push = push_client

    async def dispatch(
        self,
        *,
        user_id: str,
        user_email: str,
        event: NotificationEvent,
        context: dict[str, Any],
    ) -> None:
        """Send the email + push for this event. Each channel failure
        is caught and logged so neither blocks the other."""
        await self._maybe_email(
            user_email=user_email, event=event, context=context,
        )
        await self._maybe_push(
            user_id=user_id, event=event, context=context,
        )

    async def _maybe_email(
        self, *, user_email: str, event: NotificationEvent, context: dict[str, Any]
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
        try:
            await self._email.send_text(
                to=user_email, subject=subject, html=html, text=text,
            )
        except Exception as exc:
            log.warning(
                "notify: email send failed event=%s to=%s err=%s",
                event.value, user_email, exc,
            )

    async def _maybe_push(
        self, *, user_id: str, event: NotificationEvent, context: dict[str, Any]
    ) -> None:
        copy = _push_copy(event, context)
        if copy is None:
            return
        try:
            await self._push.send(
                user_id=user_id,
                title=copy.title,
                body=copy.body,
                data={"event": event.value, "reference": str(context.get("reference", ""))},
            )
        except Exception as exc:
            log.warning(
                "notify: push send failed event=%s user=%s err=%s",
                event.value, user_id, exc,
            )


def _email_subject(event: NotificationEvent, ctx: dict[str, Any]) -> str:
    if event is NotificationEvent.bill_success:
        if ctx.get("partial"):
            return f"Partial delivery — ₦{ctx.get('delivered_amount')} sent"
        return f"Your ₦{ctx.get('amount')} {ctx.get('tx_type_label')} is on its way"
    if event is NotificationEvent.bill_failure_refund:
        return f"Refund: ₦{ctx.get('amount')} back in your wallet"
    if event is NotificationEvent.wallet_funded:
        return f"Wallet funded — ₦{ctx.get('amount')}"
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


_TX_TYPE_LABELS = {
    "airtime":     "airtime",
    "data":        "data",
    "electricity": "electricity token",
    "cable":       "cable TV",
    "flight":      "flight",
}
