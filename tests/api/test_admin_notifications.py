"""Task 14 — admin notification-log read (PRD §16).

Read-only, paginated view over ``notification_logs`` so the admin dashboard
(T18 notifications page) can surface the delivery audit trail. Filterable by
channel/status/event/user; bad enum filters are 400 ``INVALID_FILTER``, bad
limit/offset are free 422s from the typed Query params.
"""
import pytest

from app.db.models.notification_log import (
    NotificationChannel,
    NotificationLog,
    NotificationLogStatus,
)
from app.db.models.user import User


def _seed_user(db, *, full_name="Ada Customer", email="ada@x.com", phone="+2348030000000"):
    u = User(email=email, phone=phone, full_name=full_name, password_hash="x")
    db.add(u); db.commit(); db.refresh(u)
    return u


def _seed_log(
    db, *, user_id=None, event="bill.success", channel=NotificationChannel.push,
    status=NotificationLogStatus.sent, provider="fcm",
    provider_reference="ref-1", error=None,
):
    log = NotificationLog(
        user_id=user_id, event=event, channel=channel, status=status,
        provider=provider, provider_reference=provider_reference, error=error,
    )
    db.add(log); db.commit(); db.refresh(log)
    return log


@pytest.mark.asyncio
async def test_notifications_requires_auth(admin_client):
    r = await admin_client.get("/api/v1/admin/notifications")
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "ADMIN_AUTH_REQUIRED"


@pytest.mark.asyncio
async def test_notifications_list(admin_ctx, login_admin):
    client, db, _redis = admin_ctx
    await login_admin()
    u = _seed_user(db)
    _seed_log(
        db, user_id=u.id, event="bill.success",
        channel=NotificationChannel.push, status=NotificationLogStatus.sent,
        provider="fcm", provider_reference="ref-push",
    )
    _seed_log(
        db, event="otp.send", channel=NotificationChannel.email,
        status=NotificationLogStatus.failed, provider="ses",
        provider_reference=None, error="bounce",
    )
    _seed_log(
        db, user_id=u.id, event="otp.send", channel=NotificationChannel.sms,
        status=NotificationLogStatus.pending, provider="termii",
    )

    r = await client.get("/api/v1/admin/notifications")
    assert r.status_code == 200
    data = r.json()["data"]
    assert {"items", "total", "limit", "offset"} <= data.keys()
    assert data["total"] == 3
    assert len(data["items"]) == 3

    item = data["items"][0]
    assert {
        "id", "user_id", "event", "channel", "status", "provider",
        "provider_reference", "error", "created_at", "sent_at",
    } <= item.keys()

    # The unauthenticated/system row (no user) carries a null user_id.
    by_event = {(i["event"], i["channel"]): i for i in data["items"]}
    email_row = by_event[("otp.send", "email")]
    assert email_row["user_id"] is None
    assert email_row["status"] == "failed"
    assert email_row["error"] == "bounce"
    assert email_row["provider_reference"] is None


@pytest.mark.asyncio
async def test_notifications_filter_channel(admin_ctx, login_admin):
    client, db, _redis = admin_ctx
    await login_admin()
    _seed_log(db, channel=NotificationChannel.email, provider="ses")
    _seed_log(db, channel=NotificationChannel.push, provider="fcm")

    r = await client.get("/api/v1/admin/notifications?channel=email")
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["total"] == 1
    assert all(i["channel"] == "email" for i in data["items"])

    bad = await client.get("/api/v1/admin/notifications?channel=bogus")
    assert bad.status_code == 400
    assert bad.json()["error"]["code"] == "INVALID_FILTER"


@pytest.mark.asyncio
async def test_notifications_filter_status(admin_ctx, login_admin):
    client, db, _redis = admin_ctx
    await login_admin()
    _seed_log(db, status=NotificationLogStatus.sent)
    _seed_log(db, status=NotificationLogStatus.failed, error="boom")

    r = await client.get("/api/v1/admin/notifications?status=failed")
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["total"] == 1
    assert all(i["status"] == "failed" for i in data["items"])

    bad = await client.get("/api/v1/admin/notifications?status=bogus")
    assert bad.status_code == 400
    assert bad.json()["error"]["code"] == "INVALID_FILTER"


@pytest.mark.asyncio
async def test_notifications_invalid_limit_422(admin_ctx, login_admin):
    client, db, _redis = admin_ctx
    await login_admin()
    r = await client.get("/api/v1/admin/notifications?limit=0")
    assert r.status_code == 422
