"""API-level tests for PATCH /admin/users/{user_id}.

Ops-console action: an admin edits a user's basic identity fields (name,
email, phone) from the user-profile page. Auth mirrors the refund /
resend-verification endpoints: opaque admin session cookie + double-submit
CSRF, the actor is an ``AdminUser`` (via ``login_admin``), the target is a
separate regular ``User``.

Covers:
  1. Name change -> 200, fresh user-detail payload returned.
  2. Email change -> flips ``email_verified`` to False.
  3. Phone change -> flips ``phone_verified`` to False AND revokes tokens
     (``tokens_revoked_at`` stamped + refresh keyspace cleared).
  4. Duplicate email -> 409 EMAIL_ALREADY_IN_USE.
  5. Duplicate phone -> 409 PHONE_ALREADY_IN_USE.
  6. Invalid phone -> 422 INVALID_PHONE.
  7. Unknown user -> 404 USER_NOT_FOUND.
  8. Empty body -> 400 NO_FIELDS.
  9. Missing CSRF -> 403 CSRF_FAILED.
 10. Unauthenticated -> 401 ADMIN_AUTH_REQUIRED.
"""
import uuid

import pytest

from app.core.security import hash_password
from app.db.models.user import User


def _seed_user(
    db,
    *,
    email: str,
    phone: str | None = None,
    full_name: str = "Edit Target",
    email_verified: bool = True,
    is_phone_verified: bool = True,
) -> User:
    """Seed a regular ``User`` (the edit target, distinct from the admin
    actor authenticated via ``login_admin``)."""
    user = User(
        email=email,
        phone=phone or f"+23480{uuid.uuid4().int % 10**8:08d}",
        full_name=full_name,
        password_hash=hash_password("Secret1!"),
        email_verified=email_verified,
        is_phone_verified=is_phone_verified,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


# -- 1. Name change -> fresh detail payload -------------------------------


@pytest.mark.asyncio
async def test_update_name_returns_detail(admin_ctx, login_admin):
    client, db, _redis = admin_ctx
    csrf = await login_admin()
    user = _seed_user(db, email="name@e.co", full_name="Old Name")

    r = await client.patch(
        f"/api/v1/admin/users/{user.id}",
        headers={"X-CSRF-Token": csrf},
        json={"full_name": "  New Name  "},
    )
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    # Stripped and applied.
    assert data["full_name"] == "New Name"
    # Same shape as admin_user_detail so the FE can swap state directly.
    assert data["id"] == str(user.id)
    assert "wallet_balance" in data
    assert "recent_transactions" in data
    assert "referral" in data


# -- 2. Email change flips email_verified ---------------------------------


@pytest.mark.asyncio
async def test_update_email_flips_email_verified_false(admin_ctx, login_admin):
    client, db, _redis = admin_ctx
    csrf = await login_admin()
    user = _seed_user(db, email="before@e.co", email_verified=True)

    r = await client.patch(
        f"/api/v1/admin/users/{user.id}",
        headers={"X-CSRF-Token": csrf},
        json={"email": "After@E.co"},
    )
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    # Normalised (lowercased) on write.
    assert data["email"] == "after@e.co"
    assert data["email_verified"] is False

    db.refresh(user)
    assert user.email == "after@e.co"
    assert user.email_verified is False


# -- 3. Phone change flips phone_verified AND revokes tokens ---------------


@pytest.mark.asyncio
async def test_update_phone_flips_verified_and_revokes_tokens(admin_ctx, login_admin):
    client, db, redis = admin_ctx
    csrf = await login_admin()
    user = _seed_user(db, email="phone@e.co", phone="+2348030000000", is_phone_verified=True)
    # Seed a live refresh-token key so we can prove revoke_all cleared it.
    await redis.set(f"refresh:{user.id}:jti-1", "1")

    r = await client.patch(
        f"/api/v1/admin/users/{user.id}",
        headers={"X-CSRF-Token": csrf},
        json={"phone": "08031234567"},
    )
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["phone"] == "+2348031234567"
    assert data["phone_verified"] is False

    db.refresh(user)
    assert user.phone == "+2348031234567"
    assert user.is_phone_verified is False
    assert user.tokens_revoked_at is not None
    # revoke_all cleared the refresh keyspace for this user.
    assert await redis.get(f"refresh:{user.id}:jti-1") is None


# -- 4. Duplicate email -> 409 --------------------------------------------


@pytest.mark.asyncio
async def test_duplicate_email_409(admin_ctx, login_admin):
    client, db, _redis = admin_ctx
    csrf = await login_admin()
    _seed_user(db, email="taken@e.co")
    mover = _seed_user(db, email="mover@e.co")

    r = await client.patch(
        f"/api/v1/admin/users/{mover.id}",
        headers={"X-CSRF-Token": csrf},
        json={"email": "TAKEN@e.co"},
    )
    assert r.status_code == 409, r.text
    assert r.json()["error"]["code"] == "EMAIL_ALREADY_IN_USE"


# -- 5. Duplicate phone -> 409 --------------------------------------------


@pytest.mark.asyncio
async def test_duplicate_phone_409(admin_ctx, login_admin):
    client, db, _redis = admin_ctx
    csrf = await login_admin()
    _seed_user(db, email="a@e.co", phone="+2348039999999")
    mover = _seed_user(db, email="b@e.co", phone="+2348038888888")

    r = await client.patch(
        f"/api/v1/admin/users/{mover.id}",
        headers={"X-CSRF-Token": csrf},
        json={"phone": "08039999999"},
    )
    assert r.status_code == 409, r.text
    assert r.json()["error"]["code"] == "PHONE_ALREADY_IN_USE"


# -- 6. Invalid phone -> 422 ----------------------------------------------


@pytest.mark.asyncio
async def test_invalid_phone_422(admin_ctx, login_admin):
    client, db, _redis = admin_ctx
    csrf = await login_admin()
    user = _seed_user(db, email="badphone@e.co")

    r = await client.patch(
        f"/api/v1/admin/users/{user.id}",
        headers={"X-CSRF-Token": csrf},
        json={"phone": "12345"},
    )
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "INVALID_PHONE"


# -- 7. Unknown user -> 404 -----------------------------------------------


@pytest.mark.asyncio
async def test_unknown_user_404(admin_ctx, login_admin):
    client, _db, _redis = admin_ctx
    csrf = await login_admin()

    r = await client.patch(
        f"/api/v1/admin/users/{uuid.uuid4()}",
        headers={"X-CSRF-Token": csrf},
        json={"full_name": "Nobody Home"},
    )
    assert r.status_code == 404, r.text
    assert r.json()["error"]["code"] == "USER_NOT_FOUND"


# -- 8. Empty body -> 400 NO_FIELDS ---------------------------------------


@pytest.mark.asyncio
async def test_empty_body_400_no_fields(admin_ctx, login_admin):
    client, db, _redis = admin_ctx
    csrf = await login_admin()
    user = _seed_user(db, email="empty@e.co")

    r = await client.patch(
        f"/api/v1/admin/users/{user.id}",
        headers={"X-CSRF-Token": csrf},
        json={},
    )
    assert r.status_code == 400, r.text
    assert r.json()["error"]["code"] == "NO_FIELDS"


# -- 9. Missing CSRF -> 403 -----------------------------------------------


@pytest.mark.asyncio
async def test_missing_csrf_403(admin_ctx, login_admin):
    client, db, _redis = admin_ctx
    await login_admin()
    user = _seed_user(db, email="nocsrf@e.co")

    r = await client.patch(
        f"/api/v1/admin/users/{user.id}",
        json={"full_name": "No Csrf"},
    )
    assert r.status_code == 403, r.text
    assert r.json()["error"]["code"] == "CSRF_FAILED"


# -- 10. Unauthenticated -> 401 -------------------------------------------


@pytest.mark.asyncio
async def test_unauthenticated_401(admin_ctx):
    client, db, _redis = admin_ctx  # no login_admin -> no session cookie
    user = _seed_user(db, email="noauth@e.co")

    r = await client.patch(
        f"/api/v1/admin/users/{user.id}",
        headers={"X-CSRF-Token": "whatever"},
        json={"full_name": "No Auth"},
    )
    assert r.status_code == 401, r.text
    assert r.json()["error"]["code"] == "ADMIN_AUTH_REQUIRED"
