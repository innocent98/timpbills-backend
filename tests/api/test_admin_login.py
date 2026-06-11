"""API tests for /admin/login + /admin/logout — opaque session cookies.

Covers the three contract corners:
  1. Bad credentials → 401 ADMIN_INVALID_CREDENTIALS.
  2. Good credentials → 200, sets admin_session + admin_csrf cookies,
     returns the admin payload + csrf token; logout then 200s.
  3. Disabled admin → 403 ADMIN_DISABLED.
"""
import pytest

from app.core.security import hash_password
from app.db.models.admin_user import AdminUser


def _seed_admin(db, email="ops@x.com", password="s3cret-pass"):
    admin = AdminUser(
        email=email, password_hash=hash_password(password), full_name="Ops"
    )
    db.add(admin)
    db.commit()
    db.refresh(admin)
    return admin


@pytest.mark.asyncio
async def test_login_bad_credentials_401(admin_ctx):
    client, db, _redis = admin_ctx
    _seed_admin(db)
    r = await client.post(
        "/api/v1/admin/login", json={"email": "ops@x.com", "password": "wrong"}
    )
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "ADMIN_INVALID_CREDENTIALS"


@pytest.mark.asyncio
async def test_login_sets_cookies_and_logout(admin_ctx):
    client, db, _redis = admin_ctx
    _seed_admin(db)
    r = await client.post(
        "/api/v1/admin/login",
        json={"email": "ops@x.com", "password": "s3cret-pass"},
    )
    assert r.status_code == 200
    assert "admin_session" in r.cookies
    assert "admin_csrf" in r.cookies
    body = r.json()["data"]
    assert body["admin"]["email"] == "ops@x.com"
    assert body["csrf_token"] == r.cookies["admin_csrf"]

    csrf = r.cookies["admin_csrf"]
    r2 = await client.post(
        "/api/v1/admin/logout", headers={"X-CSRF-Token": csrf}
    )
    assert r2.status_code == 200


@pytest.mark.asyncio
async def test_login_disabled_admin_403(admin_ctx):
    client, db, _redis = admin_ctx
    admin = _seed_admin(db, email="off@x.com")
    admin.is_active = False
    db.commit()
    r = await client.post(
        "/api/v1/admin/login",
        json={"email": "off@x.com", "password": "s3cret-pass"},
    )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "ADMIN_DISABLED"
