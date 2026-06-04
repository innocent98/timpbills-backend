import pytest


@pytest.mark.asyncio
async def test_overview_requires_auth(admin_client):
    r = await admin_client.get("/api/v1/admin/overview")
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "ADMIN_AUTH_REQUIRED"


@pytest.mark.asyncio
async def test_overview_authed_returns_metrics(admin_ctx, login_admin):
    client, db, _redis = admin_ctx
    await login_admin()
    r = await client.get("/api/v1/admin/overview")
    assert r.status_code == 200
    data = r.json()["data"]
    assert "success_rate" in data
    assert "daily_volume" in data
    assert "needs_attention" in data
    assert data["needs_attention"]["refunds_awaiting"] == 0
