"""Termii client — base-URL resolution, channel selection, OTP body
template, and in-band error handling.

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

import httpx
import pytest

from app.integrations.base import SmsSendError
from app.integrations.termii.client import TermiiClient, _send_url, _to_msisdn


@pytest.mark.parametrize(
    "stored,expected",
    [
        ("+2349066128757", "2349066128757"),  # E.164 → strip '+'
        ("2349066128757", "2349066128757"),   # already international
        ("09066128757", "2349066128757"),     # local NG → international
        ("  +2348011111111 ", "2348011111111"),  # whitespace tolerated
    ],
)
def test_to_msisdn_formats_for_termii(stored, expected):
    assert _to_msisdn(stored) == expected


def _ok_body() -> dict:
    """A documented Termii success envelope."""
    return {
        "code": "ok",
        "message_id": "9122821270554876574",
        "message": "Successfully Sent",
        "balance": 1047.57,
    }


def _patch_async_client(*, json_body=None, status_code=200, raise_exc=None):
    """Return (patcher, captured) — patches the AsyncClient referenced by
    the termii client module so its ``async with httpx.AsyncClient(...)``
    yields our stub. The stub's ``.post`` is an AsyncMock; we expose the
    captured payload via the returned dict after the call.

    ``json_body`` is the dict the fake response's ``.json()`` returns
    (defaults to a Termii success body). ``status_code`` drives
    ``raise_for_status``. ``raise_exc`` makes ``.post`` raise instead of
    returning (to simulate a network error)."""
    captured: dict = {}
    body = _ok_body() if json_body is None else json_body

    async def fake_post(url, json=None, **kw):
        captured["url"] = url
        captured["payload"] = json
        if raise_exc is not None:
            raise raise_exc
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = status_code
        resp.text = str(body)
        resp.json = MagicMock(return_value=body)
        if status_code >= 400:
            def _raise():
                raise httpx.HTTPStatusError(
                    "err", request=MagicMock(), response=resp,
                )
            resp.raise_for_status = MagicMock(side_effect=_raise)
        else:
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


# ── Base-URL resolution ──────────────────────────────────────────────────

def test_send_url_appends_api_path_to_bare_host(monkeypatch):
    monkeypatch.setattr(
        "app.integrations.termii.client.settings.TERMII_BASE_URL",
        "https://v3.api.termii.com",
    )
    assert _send_url() == "https://v3.api.termii.com/api/sms/send"


def test_send_url_normalises_trailing_api_suffix(monkeypatch):
    """A base that already carries ``/api`` must not double it."""
    monkeypatch.setattr(
        "app.integrations.termii.client.settings.TERMII_BASE_URL",
        "https://api.ng.termii.com/api",
    )
    assert _send_url() == "https://api.ng.termii.com/api/sms/send"


def test_send_url_honours_configured_base(monkeypatch):
    """The hardcoded-base bug is gone: a custom base is respected."""
    monkeypatch.setattr(
        "app.integrations.termii.client.settings.TERMII_BASE_URL",
        "https://staging.example.com/",
    )
    assert _send_url() == "https://staging.example.com/api/sms/send"


@pytest.mark.asyncio
async def test_send_otp_posts_to_configured_base(monkeypatch):
    monkeypatch.setattr(
        "app.integrations.termii.client.settings.TERMII_BASE_URL",
        "https://v3.api.termii.com",
    )
    monkeypatch.setattr(
        "app.integrations.termii.client.settings.TERMII_API_KEY", "k",
    )
    client = TermiiClient()
    patcher, captured = _patch_async_client()
    with patcher:
        await client.send_otp(phone="+2348011111111", code="123456")
    assert captured["url"] == "https://v3.api.termii.com/api/sms/send"


# ── Channel + payload shape ──────────────────────────────────────────────

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
    # Termii requires international format with NO leading '+'.
    assert captured["payload"]["to"] == "2348011111111"
    assert captured["payload"]["sms"].startswith(
        "Your TimpBills verification code is 123456"
    )
    assert "expires in 30 minutes" in captured["payload"]["sms"]
    assert captured["payload"]["type"] == "plain"
    assert captured["payload"]["api_key"] == "k"


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


@pytest.mark.asyncio
async def test_sender_id_falls_back_to_default(monkeypatch):
    monkeypatch.setattr(
        "app.integrations.termii.client.settings.TERMII_SENDER_ID", None,
    )
    monkeypatch.setattr(
        "app.integrations.termii.client.settings.TERMII_API_KEY", "k",
    )
    client = TermiiClient()
    patcher, captured = _patch_async_client()
    with patcher:
        await client.send_otp(phone="+2348011111111", code="123456")
    assert captured["payload"]["from"] == "N-Alert"


# ── In-band + transport error handling ───────────────────────────────────

@pytest.mark.asyncio
async def test_send_otp_success_does_not_raise(monkeypatch):
    monkeypatch.setattr(
        "app.integrations.termii.client.settings.TERMII_API_KEY", "k",
    )
    client = TermiiClient()
    patcher, _ = _patch_async_client(json_body=_ok_body())
    with patcher:
        await client.send_otp(phone="+2348011111111", code="123456")  # no raise


@pytest.mark.asyncio
async def test_in_band_insufficient_balance_raises(monkeypatch):
    """HTTP 200 with an error body (no message_id) must raise SmsSendError
    so the OTP funnel marks the notification_logs row failed."""
    monkeypatch.setattr(
        "app.integrations.termii.client.settings.TERMII_API_KEY", "k",
    )
    client = TermiiClient()
    patcher, _ = _patch_async_client(
        json_body={"message": "Insufficient balance"}, status_code=200,
    )
    with patcher, pytest.raises(SmsSendError, match="Insufficient balance"):
        await client.send_otp(phone="+2348011111111", code="123456")


@pytest.mark.asyncio
async def test_in_band_unapproved_sender_raises(monkeypatch):
    monkeypatch.setattr(
        "app.integrations.termii.client.settings.TERMII_API_KEY", "k",
    )
    client = TermiiClient()
    patcher, _ = _patch_async_client(
        json_body={"code": "invalid_sender_id", "message": "Invalid Sender Id"},
        status_code=200,
    )
    with patcher, pytest.raises(SmsSendError, match="Invalid Sender Id"):
        await client.send_otp(phone="+2348011111111", code="123456")


@pytest.mark.asyncio
async def test_http_4xx_raises(monkeypatch):
    monkeypatch.setattr(
        "app.integrations.termii.client.settings.TERMII_API_KEY", "k",
    )
    client = TermiiClient()
    patcher, _ = _patch_async_client(
        json_body={"message": "No valid API key provided"}, status_code=401,
    )
    with patcher, pytest.raises(SmsSendError, match="http 401"):
        await client.send_otp(phone="+2348011111111", code="123456")


@pytest.mark.asyncio
async def test_network_error_raises(monkeypatch):
    monkeypatch.setattr(
        "app.integrations.termii.client.settings.TERMII_API_KEY", "k",
    )
    client = TermiiClient()
    patcher, _ = _patch_async_client(
        raise_exc=httpx.ConnectError("boom"),
    )
    with patcher, pytest.raises(SmsSendError, match="network error"):
        await client.send_otp(phone="+2348011111111", code="123456")
