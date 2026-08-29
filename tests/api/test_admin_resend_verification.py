"""API-level tests for POST /admin/users/{user_id}/resend-verification-email.

Ops-console action: an admin triggers a fresh email-verification OTP for a
user whose email is still unverified (from the user-profile page).

Auth mirrors the refund endpoint — opaque admin session cookie + double-submit
CSRF, actor is an ``AdminUser`` (via ``login_admin``), target is a separate
regular ``User``. The email side is faked by overriding ``get_email_provider``
on top of the shared ``admin_ctx`` client so we can assert on the outbox.

Covers:
  1. Unverified user → 200 ``sent=true`` and one OTP email queued.
  2. Already-verified user → 200 ``sent=false, reason=already_verified``,
     no email queued (idempotent-friendly, not an error).
  3. Unknown user_id → 404 ``USER_NOT_FOUND``.
"""
import uuid

import pytest
import pytest_asyncio

from app.api.deps import get_email_provider
from app.core.security import hash_password
from app.db.models.user import User
from app.integrations.email.fake import FakeEmailClient
from app.main import app


def _seed_user(db, *, email: str, email_verified: bool) -> User:
    """Seed a regular ``User`` (the resend target — distinct from the admin
    actor authenticated via ``login_admin``)."""
    user = User(
        email=email,
        phone=f"+23480{uuid.uuid4().int % 10**8:08d}",
        full_name="Resend Target",
        password_hash=hash_password("Secret1!"),
        email_verified=email_verified,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@pytest_asyncio.fixture
async def admin_email_ctx(admin_ctx):
    """``admin_ctx`` with ``get_email_provider`` overridden to a fresh
    ``FakeEmailClient`` so ``send_email_otp`` records into an inspectable
    outbox. Yields ``(client, db, fake_email_client)``.

    The override is added on top of the overrides ``admin_ctx`` already set;
    its own teardown ``dependency_overrides.clear()`` cleans this up too.
    """
    client, db, _redis = admin_ctx
    fake_email = FakeEmailClient()
    app.dependency_overrides[get_email_provider] = lambda: fake_email
    return client, db, fake_email


# ── 1. Unverified user → email queued ───────────────────────────────────


@pytest.mark.asyncio
async def test_resend_unverified_user_sends_email(admin_email_ctx, login_admin):
    client, db, fake_email = admin_email_ctx
    csrf = await login_admin()
    user = _seed_user(db, email="unverified@e.co", email_verified=False)

    r = await client.post(
        f"/api/v1/admin/users/{user.id}/resend-verification-email",
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 200, r.text
    body = r.json()["data"]
    assert body["sent"] is True
    assert body["email"] == "unverified@e.co"

    # Exactly one OTP email was queued to the target address.
    assert len(fake_email.sent) == 1
    assert fake_email.sent[0].to == "unverified@e.co"


# ── 2. Already-verified user → no-op, no email ──────────────────────────


@pytest.mark.asyncio
async def test_resend_already_verified_user_is_noop(admin_email_ctx, login_admin):
    client, db, fake_email = admin_email_ctx
    csrf = await login_admin()
    user = _seed_user(db, email="verified@e.co", email_verified=True)

    r = await client.post(
        f"/api/v1/admin/users/{user.id}/resend-verification-email",
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 200, r.text
    body = r.json()["data"]
    assert body["sent"] is False
    assert body["reason"] == "already_verified"
    assert body["email"] == "verified@e.co"

    # No email queued — the endpoint short-circuits before send_email_otp.
    assert fake_email.sent == []


# ── 3. Unknown user_id → 404 ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_resend_unknown_user_404(admin_email_ctx, login_admin):
    client, _db, fake_email = admin_email_ctx
    csrf = await login_admin()

    r = await client.post(
        f"/api/v1/admin/users/{uuid.uuid4()}/resend-verification-email",
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "USER_NOT_FOUND"
    assert fake_email.sent == []
