"""Unit tests for NotificationPreference-aware gating in NotificationService
(Sprint 5c · Task 5.2).

Covers:
  * The DEFAULT (no NotificationPreference row exists) — transactional + referral
    + email default ON; promotions default OFF.
  * Explicit prefs row with a channel set to False suppresses the corresponding
    push / email for that event category.
  * Email-channel gating respects ``email_notifications`` regardless of the
    push-channel flag (they are independent).
  * The category mapping covers every NotificationEvent in the enum (so a
    newly-added event without an updated mapping breaks loudly).

NotificationService is invoked synchronously (asyncio.run) — same shape as
``tests/services/test_notification_service.py``. We exercise the gating
through the public ``dispatch`` API, not by reaching into private helpers,
so the test pins behaviour not implementation.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.models.notification_preference import NotificationPreference
from app.db.models.user import KycLevel, User
from app.integrations.email.fake import FakeEmailClient
from app.integrations.push.fake import FakePushClient
from app.services.notification_service import (
    EVENT_CATEGORY,
    NotificationCategory,
    NotificationEvent,
    NotificationService,
    build_bill_context,
    build_wallet_funded_context,
)


@pytest.fixture
def db_session():
    """In-memory SQLite session — mirrors the conftest db_session fixture
    so the gating tests don't depend on the test-wide fixture (the file
    lives in tests/services/ which doesn't conftest-import it)."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _make_user(db, *, email: str, phone: str) -> User:
    u = User(
        email=email,
        phone=phone,
        full_name="Pref User",
        password_hash="x",
        kyc_level=KycLevel.tier_0,
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def _bill_ctx() -> dict:
    return build_bill_context(
        tx_type="airtime", amount=Decimal("500"),
        destination="08012345678", reference="TMP-X",
        when="now", partial=False,
    )


def _wallet_ctx() -> dict:
    return build_wallet_funded_context(
        amount=Decimal("5000"), balance=Decimal("10000"),
        reference="FUND-1", channel="card",
    )


# ─── Category map ───────────────────────────────────────────────────────────


def test_event_category_map_covers_every_event():
    """A new NotificationEvent without an EVENT_CATEGORY entry is a silent
    bug — it would default to "no gating" and bypass user preferences.
    Pin the invariant here so the next event addition fails loudly."""
    missing = [e for e in NotificationEvent if e not in EVENT_CATEGORY]
    assert missing == [], (
        f"NotificationEvent(s) missing from EVENT_CATEGORY: {missing}. "
        "Add them to app/services/notification_service.py::EVENT_CATEGORY."
    )


def test_event_category_values_are_valid_enum_members():
    for evt, category in EVENT_CATEGORY.items():
        assert isinstance(category, NotificationCategory), (
            f"{evt} maps to {category!r} which is not a NotificationCategory"
        )


# ─── Default behaviour — no NotificationPreference row exists ───────────────


def test_no_prefs_row_transaction_push_fires(db_session):
    """A user who has never touched the preferences screen should still
    receive transactional pushes — that's the spec §3.2 default and the
    one channel mobile relies on for receipts."""
    user = _make_user(db_session, email="def-tx@t.co", phone="+2348011110001")
    push = FakePushClient()
    email = FakeEmailClient()
    svc = NotificationService(
        email_client=email, push_client=push, db=db_session,
    )
    asyncio.run(svc.dispatch(
        user_id=str(user.id), user_email=user.email,
        event=NotificationEvent.bill_success, context=_bill_ctx(),
    ))
    assert len(push.sent) == 1
    assert len(email.sent) == 1


def test_no_prefs_row_referral_push_fires(db_session):
    user = _make_user(db_session, email="def-ref@t.co", phone="+2348011110002")
    push = FakePushClient()
    svc = NotificationService(
        email_client=FakeEmailClient(), push_client=push, db=db_session,
    )
    asyncio.run(svc.dispatch(
        user_id=str(user.id), user_email=user.email,
        event=NotificationEvent.referral_credited,
        context={"amount_naira": "500", "reference": "R-1"},
    ))
    assert len(push.sent) == 1


# ─── Explicit prefs gating ──────────────────────────────────────────────────


def _set_prefs(db, user_id: UUID, **flags) -> NotificationPreference:
    prefs = NotificationPreference(user_id=user_id, **flags)
    db.add(prefs)
    db.commit()
    return prefs


def test_transaction_push_suppressed_when_disabled(db_session):
    user = _make_user(db_session, email="off-tx@t.co", phone="+2348011110003")
    _set_prefs(
        db_session, user.id,
        transaction_alerts=False, referral_updates=True,
        promotions=False, email_notifications=True,
    )
    push = FakePushClient()
    svc = NotificationService(
        email_client=FakeEmailClient(), push_client=push, db=db_session,
    )
    asyncio.run(svc.dispatch(
        user_id=str(user.id), user_email=user.email,
        event=NotificationEvent.bill_success, context=_bill_ctx(),
    ))
    assert push.sent == [], "transaction_alerts=False must suppress push"


def test_wallet_funded_push_suppressed_when_transaction_alerts_off(db_session):
    """wallet_funded is a transactional event — same gate as bill_success."""
    user = _make_user(db_session, email="off-wallet@t.co", phone="+2348011110004")
    _set_prefs(
        db_session, user.id,
        transaction_alerts=False, referral_updates=True,
        promotions=False, email_notifications=True,
    )
    push = FakePushClient()
    svc = NotificationService(
        email_client=FakeEmailClient(), push_client=push, db=db_session,
    )
    asyncio.run(svc.dispatch(
        user_id=str(user.id), user_email=user.email,
        event=NotificationEvent.wallet_funded, context=_wallet_ctx(),
    ))
    assert push.sent == []


def test_referral_push_suppressed_when_disabled(db_session):
    user = _make_user(db_session, email="off-ref@t.co", phone="+2348011110005")
    _set_prefs(
        db_session, user.id,
        transaction_alerts=True, referral_updates=False,
        promotions=False, email_notifications=True,
    )
    push = FakePushClient()
    svc = NotificationService(
        email_client=FakeEmailClient(), push_client=push, db=db_session,
    )
    for event, ctx in (
        (NotificationEvent.referrer_signup_notified,
         {"referee_display_name": "Friend A", "reference": "R"}),
        (NotificationEvent.referral_credited,
         {"amount_naira": "500", "reference": "R"}),
        (NotificationEvent.welcome_bonus,
         {"amount_naira": "300", "reference": "R"}),
    ):
        push.sent.clear()
        asyncio.run(svc.dispatch(
            user_id=str(user.id), user_email=user.email,
            event=event, context=ctx,
        ))
        assert push.sent == [], (
            f"referral_updates=False must suppress {event.value} push"
        )


def test_referral_push_disabled_does_not_block_transaction_push(db_session):
    """Channels are independent — disabling referrals must leave bill
    success notifications untouched."""
    user = _make_user(db_session, email="indie@t.co", phone="+2348011110006")
    _set_prefs(
        db_session, user.id,
        transaction_alerts=True, referral_updates=False,
        promotions=False, email_notifications=True,
    )
    push = FakePushClient()
    svc = NotificationService(
        email_client=FakeEmailClient(), push_client=push, db=db_session,
    )
    asyncio.run(svc.dispatch(
        user_id=str(user.id), user_email=user.email,
        event=NotificationEvent.bill_success, context=_bill_ctx(),
    ))
    assert len(push.sent) == 1


# ─── Email gating is independent of push gating ─────────────────────────────


def test_email_disabled_suppresses_email_but_not_push(db_session):
    user = _make_user(db_session, email="no-email@t.co", phone="+2348011110007")
    _set_prefs(
        db_session, user.id,
        transaction_alerts=True, referral_updates=True,
        promotions=False, email_notifications=False,
    )
    push = FakePushClient()
    email = FakeEmailClient()
    svc = NotificationService(
        email_client=email, push_client=push, db=db_session,
    )
    asyncio.run(svc.dispatch(
        user_id=str(user.id), user_email=user.email,
        event=NotificationEvent.bill_success, context=_bill_ctx(),
    ))
    assert len(push.sent) == 1
    assert email.sent == [], "email_notifications=False must suppress email"


# ─── Defensive: unknown user_id (e.g. stale celery task) defaults-on ────────


def test_unknown_user_id_does_not_crash_dispatch(db_session):
    """If the user row was deleted between enqueue and worker pickup, the
    prefs lookup misses and we should fall back to defaults (don't drop
    the transactional push). The dispatcher must not raise."""
    push = FakePushClient()
    svc = NotificationService(
        email_client=FakeEmailClient(), push_client=push, db=db_session,
    )
    asyncio.run(svc.dispatch(
        user_id=str(uuid4()), user_email="ghost@t.co",
        event=NotificationEvent.bill_success, context=_bill_ctx(),
    ))
    assert len(push.sent) == 1


# ─── db=None — legacy path stays default-on (back-compat) ───────────────────


def test_no_db_wired_legacy_behaviour_is_default_on():
    """The pre-Sprint-5c constructor signature (no db) must still dispatch
    every push — older callers (and the worker before the wiring lands)
    rely on this."""
    push = FakePushClient()
    svc = NotificationService(
        email_client=FakeEmailClient(), push_client=push,
    )
    asyncio.run(svc.dispatch(
        user_id=str(uuid4()), user_email="legacy@t.co",
        event=NotificationEvent.bill_success, context=_bill_ctx(),
    ))
    assert len(push.sent) == 1
