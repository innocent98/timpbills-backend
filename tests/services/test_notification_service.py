"""Unit tests for NotificationService + the Jinja2 renderer.

Exercises channel fan-out (email + push), template rendering, partial-
delivery branch in bill_success, and the robustness-against-failure
contract (a push outage must not break the email send, and vice versa).
"""
import asyncio
from decimal import Decimal

import pytest

from app.integrations.email.fake import FakeEmailClient
from app.integrations.email.renderer import render_email
from app.integrations.push.fake import FakePushClient
from app.services.notification_service import (
    NotificationEvent,
    NotificationService,
    build_bill_context,
    build_wallet_funded_context,
)


def _svc() -> tuple[NotificationService, FakeEmailClient, FakePushClient]:
    email = FakeEmailClient()
    push = FakePushClient()
    return NotificationService(email_client=email, push_client=push), email, push


# ─── Renderer ───────────────────────────────────────────────────────────


def test_renderer_returns_both_html_and_text_for_bill_success():
    html, text = render_email("bill_success", {
        "tx_type_label": "airtime",
        "amount": "500.00",
        "destination": "08012345678",
        "reference": "TMP-X",
        "when": "2026-04-22T10:00:00",
        "partial": False,
    })
    assert "500.00" in html and "airtime" in html
    assert "500.00" in text and "airtime" in text
    # HTML should be escaped Jinja2-style; no bare template markers leak.
    assert "{{" not in html and "{%" not in html


def test_renderer_partial_delivery_renders_shortfall_banner():
    html, _ = render_email("bill_success", {
        "tx_type_label": "airtime",
        "amount": "500.00",
        "destination": "08012345678",
        "reference": "TMP-X",
        "when": "now",
        "partial": True,
        "delivered_amount": "450.00",
        "shortfall": "50.00",
    })
    assert "Partial delivery" in html
    assert "450.00" in html and "50.00" in html


def test_renderer_bill_failure_shows_reason_when_present():
    html_with, _ = render_email("bill_failure_refund", {
        "tx_type_label": "airtime", "amount": "500.00",
        "destination": "080", "reference": "TMP-X",
        "reason": "Telco rejected destination number",
    })
    assert "Telco rejected" in html_with

    html_without, _ = render_email("bill_failure_refund", {
        "tx_type_label": "airtime", "amount": "500.00",
        "destination": "080", "reference": "TMP-X",
    })
    # No reason row when field is absent — the template should hide it.
    assert "Reason" not in html_without


def test_renderer_wallet_funded_renders_balance_and_channel():
    html, text = render_email("wallet_funded", {
        "amount": "5000.00", "balance": "10000.00",
        "reference": "FUND-1", "channel": "card",
    })
    assert "5000.00" in html and "10000.00" in html
    assert "card" in html and "card" in text


# ─── Dispatch: email + push fan-out ──────────────────────────────────────


def test_dispatch_bill_success_sends_both_email_and_push():
    svc, email, push = _svc()
    ctx = build_bill_context(
        tx_type="airtime", amount=Decimal("500"),
        destination="08012345678", reference="TMP-X",
        when="now", partial=False,
    )
    asyncio.run(svc.dispatch(
        user_id="u1", user_email="a@t.co",
        event=NotificationEvent.bill_success, context=ctx,
    ))
    assert len(email.sent) == 1
    assert email.sent[0].to == "a@t.co"
    assert "airtime" in email.sent[0].subject.lower() or "airtime" in email.sent[0].subject
    assert len(push.sent) == 1
    assert push.sent[0].user_id == "u1"
    assert push.sent[0].data["event"] == "bill_success"
    assert push.sent[0].data["reference"] == "TMP-X"


def test_dispatch_bill_failure_refund_sends_both_channels():
    svc, email, push = _svc()
    ctx = build_bill_context(
        tx_type="data", amount=Decimal("1000"),
        destination="08012345678", reference="TMP-Y",
        when="now", partial=False,
    )
    ctx["reason"] = "invalid destination"
    asyncio.run(svc.dispatch(
        user_id="u2", user_email="b@t.co",
        event=NotificationEvent.bill_failure_refund, context=ctx,
    ))
    assert len(email.sent) == 1
    assert "refund" in email.sent[0].subject.lower() or "Refund" in email.sent[0].subject
    assert len(push.sent) == 1
    assert "wallet" in push.sent[0].body.lower()


def test_dispatch_wallet_funded_sends_both_channels():
    svc, email, push = _svc()
    ctx = build_wallet_funded_context(
        amount=Decimal("5000"), balance=Decimal("12000"),
        reference="FUND-1", channel="card",
    )
    asyncio.run(svc.dispatch(
        user_id="u3", user_email="c@t.co",
        event=NotificationEvent.wallet_funded, context=ctx,
    ))
    assert len(email.sent) == 1
    assert "funded" in email.sent[0].subject.lower()
    assert len(push.sent) == 1


def test_dispatch_partial_bill_success_shows_partial_copy_in_push():
    svc, email, push = _svc()
    ctx = build_bill_context(
        tx_type="airtime", amount=Decimal("500"),
        destination="08012345678", reference="TMP-P",
        when="now", partial=True,
        delivered_amount=Decimal("450"), shortfall=Decimal("50"),
    )
    asyncio.run(svc.dispatch(
        user_id="u4", user_email="d@t.co",
        event=NotificationEvent.bill_success, context=ctx,
    ))
    assert len(email.sent) == 1
    assert "partial" in email.sent[0].subject.lower()
    assert len(push.sent) == 1
    # The push title signals partial delivery so lock-screen readers
    # don't assume full success.
    assert "partial" in push.sent[0].title.lower()


# ─── Skip paths ─────────────────────────────────────────────────────────


def test_dispatch_skips_email_when_user_email_is_blank():
    svc, email, push = _svc()
    asyncio.run(svc.dispatch(
        user_id="u5", user_email="",   # unverified user edge case
        event=NotificationEvent.bill_success,
        context=build_bill_context(
            tx_type="airtime", amount=Decimal("100"),
            destination="080", reference="R",
            when="now", partial=False,
        ),
    ))
    assert len(email.sent) == 0
    # Push still lands — it doesn't need an email address.
    assert len(push.sent) == 1


def test_dispatch_refund_complete_event_is_a_noop_placeholder():
    """refund_complete is reserved for a future flow; dispatching it
    today is a silent no-op on both channels so we don't crash if
    someone wires it early."""
    svc, email, push = _svc()
    asyncio.run(svc.dispatch(
        user_id="u6", user_email="e@t.co",
        event=NotificationEvent.refund_complete,
        context={"reference": "R"},
    ))
    assert len(email.sent) == 0
    assert len(push.sent) == 0


# ─── Robustness: one channel failing doesn't block the other ────────────


class _BrokenPush:
    async def send(self, **kwargs):
        raise RuntimeError("fcm unavailable")


class _BrokenEmail:
    async def send_otp(self, **kwargs): pass
    async def send_text(self, **kwargs):
        raise RuntimeError("resend 503")


def test_dispatch_swallows_push_failure_and_still_sends_email():
    email = FakeEmailClient()
    svc = NotificationService(email_client=email, push_client=_BrokenPush())
    asyncio.run(svc.dispatch(
        user_id="u7", user_email="f@t.co",
        event=NotificationEvent.bill_success,
        context=build_bill_context(
            tx_type="airtime", amount=Decimal("100"),
            destination="080", reference="R",
            when="now", partial=False,
        ),
    ))
    # Email still went out.
    assert len(email.sent) == 1


def test_dispatch_swallows_email_failure_and_still_sends_push():
    push = FakePushClient()
    svc = NotificationService(email_client=_BrokenEmail(), push_client=push)
    asyncio.run(svc.dispatch(
        user_id="u8", user_email="g@t.co",
        event=NotificationEvent.bill_success,
        context=build_bill_context(
            tx_type="airtime", amount=Decimal("100"),
            destination="080", reference="R",
            when="now", partial=False,
        ),
    ))
    assert len(push.sent) == 1
