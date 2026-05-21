"""API tests for /api/v1/users/me/avatar (Sprint 5c · Task 3.2).

Cloudinary is patched at ``app.services.avatar_service.cloudinary.uploader.upload``
so no network call ever happens — fake creds in settings are fine.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
import pytest_asyncio
from fakeredis.aioredis import FakeRedis
from httpx import ASGITransport, AsyncClient

from app.api.deps import (
    get_db,
    get_email_provider,
    get_token_store,
    reset_fake_email,
    reset_fake_sms,
)
from app.core.limiter import limiter
from app.integrations.email.fake import FakeEmailClient
from app.main import app
from app.services.token_store import RedisTokenStore

_test_email_client = FakeEmailClient()


@pytest_asyncio.fixture
async def client(db_session):
    def _get_db():
        try:
            yield db_session
        finally:
            pass

    fake_redis = FakeRedis(decode_responses=True)

    def _get_token_store():
        return RedisTokenStore(redis=fake_redis)

    def _get_email():
        return _test_email_client

    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[get_token_store] = _get_token_store
    app.dependency_overrides[get_email_provider] = _get_email
    reset_fake_sms()
    reset_fake_email()
    _test_email_client.sent.clear()

    limiter.enabled = False
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    limiter.enabled = True
    await fake_redis.aclose()
    app.dependency_overrides.clear()


async def _seed_user(
    client: AsyncClient, *, email: str, phone: str
) -> dict:
    """Register + verify email; return headers ready for protected calls."""
    r = await client.post(
        "/api/v1/auth/register",
        json={
            "full_name": "Avatar User",
            "phone": phone,
            "email": email,
            "password": "Secret1!",
        },
    )
    assert r.status_code == 201, r.text

    code = _test_email_client.sent[-1].code_or_body
    r2 = await client.post(
        "/api/v1/auth/email/verify", json={"email": email, "code": code}
    )
    assert r2.status_code == 200, r2.text
    tokens = r2.json()["data"]["tokens"]
    return {"Authorization": f"Bearer {tokens['access_token']}"}


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_upload_avatar_happy(client):
    headers = await _seed_user(
        client, email="av1@test.co", phone="+2348055555501"
    )

    fake_url = "https://res.cloudinary.com/x/image/upload/timpbills/avatars/u1.jpg"
    with patch(
        "app.services.avatar_service.cloudinary.uploader.upload",
        return_value={"secure_url": fake_url},
    ):
        r = await client.post(
            "/api/v1/users/me/avatar",
            files={"file": ("avatar.jpg", b"jpegbytes", "image/jpeg")},
            headers=headers,
        )
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["avatar_url"] == fake_url

    # GET /auth/me reflects the persisted URL
    g = await client.get("/api/v1/auth/me", headers=headers)
    assert g.status_code == 200
    assert g.json()["data"]["avatar_url"] == fake_url


# ---------------------------------------------------------------------------
# Validation gates
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_upload_avatar_rejects_oversize(client):
    headers = await _seed_user(
        client, email="av2@test.co", phone="+2348055555502"
    )
    huge = b"\0" * (6 * 1024 * 1024)
    r = await client.post(
        "/api/v1/users/me/avatar",
        files={"file": ("big.jpg", huge, "image/jpeg")},
        headers=headers,
    )
    assert r.status_code == 413, r.text
    assert r.json()["error"]["code"] == "AVATAR_TOO_LARGE"


@pytest.mark.asyncio
async def test_upload_avatar_rejects_wrong_mime(client):
    headers = await _seed_user(
        client, email="av3@test.co", phone="+2348055555503"
    )
    r = await client.post(
        "/api/v1/users/me/avatar",
        files={"file": ("animation.gif", b"gifbytes", "image/gif")},
        headers=headers,
    )
    assert r.status_code == 415, r.text
    assert r.json()["error"]["code"] == "AVATAR_WRONG_MIME"


@pytest.mark.asyncio
async def test_upload_avatar_translates_upstream_failure(client):
    headers = await _seed_user(
        client, email="av4@test.co", phone="+2348055555504"
    )
    with patch(
        "app.services.avatar_service.cloudinary.uploader.upload",
        side_effect=Exception("cloudinary 500"),
    ):
        r = await client.post(
            "/api/v1/users/me/avatar",
            files={"file": ("avatar.png", b"pngbytes", "image/png")},
            headers=headers,
        )
    assert r.status_code == 502, r.text
    assert r.json()["error"]["code"] == "AVATAR_UPSTREAM_FAILURE"


# ---------------------------------------------------------------------------
# Auth gate
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_upload_avatar_auth_required(client):
    r = await client.post(
        "/api/v1/users/me/avatar",
        files={"file": ("avatar.jpg", b"jpegbytes", "image/jpeg")},
    )
    assert r.status_code == 401, r.text


@pytest.mark.asyncio
async def test_delete_avatar_auth_required(client):
    r = await client.delete("/api/v1/users/me/avatar")
    assert r.status_code == 401, r.text


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_delete_avatar_clears_url(client):
    headers = await _seed_user(
        client, email="av5@test.co", phone="+2348055555505"
    )

    fake_url = "https://res.cloudinary.com/x/avatar/u.jpg"
    with patch(
        "app.services.avatar_service.cloudinary.uploader.upload",
        return_value={"secure_url": fake_url},
    ):
        up = await client.post(
            "/api/v1/users/me/avatar",
            files={"file": ("a.jpg", b"jpegbytes", "image/jpeg")},
            headers=headers,
        )
        assert up.status_code == 200

    # Delete clears the URL
    r = await client.delete("/api/v1/users/me/avatar", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["data"] == {"avatar_url": None}

    # GET /auth/me confirms the persisted null
    g = await client.get("/api/v1/auth/me", headers=headers)
    assert g.status_code == 200
    assert g.json()["data"]["avatar_url"] is None


@pytest.mark.asyncio
async def test_delete_avatar_idempotent_when_unset(client):
    """Deleting when no avatar is set is fine — still 200, still null."""
    headers = await _seed_user(
        client, email="av6@test.co", phone="+2348055555506"
    )
    r = await client.delete("/api/v1/users/me/avatar", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["data"] == {"avatar_url": None}
