"""Termii client — channel selection + OTP body template.

Patches ``app.integrations.termii.client.httpx.AsyncClient`` directly with a
context-manager stub so we capture the request payload without making a
network call. We deliberately do NOT patch ``httpx.AsyncClient.post`` at
the class level — other tests in this directory (e.g. ``test_fcm_client``)
patch httpx via dotted paths that resolve to the shared ``httpx`` module,
and a failure inside their patch context can leave ``httpx.AsyncClient``
mutated for downstream tests in the same pytest invocation. Targeting the
client-module-local attribute keeps our patch independent of that hazard.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.integrations.termii.client import TermiiClient


def _patch_async_client():
    """Return (patcher, captured) — patches the AsyncClient referenced by
    the termii client module so its ``async with httpx.AsyncClient(...)``
    yields our stub. The stub's ``.post`` is an AsyncMock; we expose the
    captured payload via the returned dict after the call."""
    captured: dict = {}

    async def fake_post(url, json=None, **kw):
        captured["url"] = url
        captured["payload"] = json
        resp = MagicMock()
        resp.raise_for_status = MagicMock(return_value=None)
        return resp

    inner = MagicMock()
    inner.post = AsyncMock(side_effect=fake_post)

    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=inner)
    ctx.__aexit__ = AsyncMock(return_value=None)

    factory = MagicMock(return_value=ctx)
    return patch(
        "app.integrations.termii.client.httpx.AsyncClient", factory,
    ), captured


@pytest.mark.asyncio
async def test_send_otp_uses_channel_from_settings(monkeypatch):
    monkeypatch.setattr(
        "app.integrations.termii.client.settings.TERMII_OTP_CHANNEL", "dnd",
    )
    monkeypatch.setattr(
        "app.integrations.termii.client.settings.TERMII_API_KEY", "k",
    )
    client = TermiiClient()
    patcher, captured = _patch_async_client()
    with patcher:
        await client.send_otp(phone="+2348011111111", code="123456")

    assert captured["payload"]["channel"] == "dnd"
    assert captured["payload"]["to"] == "+2348011111111"
    assert captured["payload"]["sms"].startswith("Your Timpbills code is 123456")
    assert "Do not share this code" in captured["payload"]["sms"]
    assert "expires in 5 minutes" in captured["payload"]["sms"]
    assert captured["payload"]["type"] == "plain"


@pytest.mark.asyncio
async def test_send_otp_respects_overridden_channel(monkeypatch):
    """If ops temporarily flip TERMII_OTP_CHANNEL back to generic
    (rollback scenario), the client should honour it."""
    monkeypatch.setattr(
        "app.integrations.termii.client.settings.TERMII_OTP_CHANNEL", "generic",
    )
    monkeypatch.setattr(
        "app.integrations.termii.client.settings.TERMII_API_KEY", "k",
    )
    client = TermiiClient()
    patcher, captured = _patch_async_client()
    with patcher:
        await client.send_otp(phone="+2348011111111", code="123456")
    assert captured["payload"]["channel"] == "generic"


@pytest.mark.asyncio
async def test_send_text_stays_on_generic_channel(monkeypatch):
    """Non-OTP texts stay on the cheaper generic channel regardless
    of TERMII_OTP_CHANNEL."""
    monkeypatch.setattr(
        "app.integrations.termii.client.settings.TERMII_OTP_CHANNEL", "dnd",
    )
    monkeypatch.setattr(
        "app.integrations.termii.client.settings.TERMII_API_KEY", "k",
    )
    client = TermiiClient()
    patcher, captured = _patch_async_client()
    with patcher:
        await client.send_text(phone="+2348011111111", message="Welcome!")
    assert captured["payload"]["channel"] == "generic"
    assert captured["payload"]["sms"] == "Welcome!"
