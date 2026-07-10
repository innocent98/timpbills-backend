"""DojahClient.fetch_verification — retry policy (code-review fix #3).

Retrying a 4xx just repeats the same rejected request (bad reference,
malformed params, auth failure) — that should fail fast, not eat 3
attempts and ~3s of backoff. Only 5xx responses and genuine transport
errors (timeouts, connection failures) are worth retrying.

Patches ``app.integrations.dojah.client.httpx.AsyncClient`` directly with a
context-manager stub — same pattern as
``tests/integrations/test_termii_channel.py`` — so we exercise the real
tenacity-decorated ``fetch_verification`` without making a network call.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.core.config import settings
from app.integrations.dojah.client import DojahClient


def _client(monkeypatch) -> DojahClient:
    monkeypatch.setattr(settings, "DOJAH_API_KEY", "test-key")
    monkeypatch.setattr(settings, "DOJAH_APP_ID", "test-app-id")
    monkeypatch.setattr(settings, "DOJAH_FACE_MATCH_THRESHOLD", 70)
    monkeypatch.setattr(settings, "DOJAH_BASE_URL", "https://dojah.test")
    return DojahClient()


def _success_payload(reference_id: str = "KYC-BVN-1") -> dict:
    return {
        "reference_id": reference_id,
        "status": "Completed",
        "verification_type": "bvn",
        "id_verification": {"verified": True},
        "liveness": {"passed": True},
        "face_match": {"match": True, "confidence": 95},
        "masked_id": "•••••••••17",
    }


def _mock_response(status_code: int, json_body: dict) -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.json = MagicMock(return_value=json_body)
    if status_code >= 400:
        def _raise():
            raise httpx.HTTPStatusError("err", request=MagicMock(), response=resp)
        resp.raise_for_status = MagicMock(side_effect=_raise)
    else:
        resp.raise_for_status = MagicMock(return_value=None)
    return resp


def _patch_async_client(outcomes: list):
    """``outcomes`` is consumed in order, one per GET call — mirrors
    tenacity's successive retry attempts. An ``Exception`` instance in the
    list is raised instead of returned (simulates a transport error)."""
    calls: list = []

    async def fake_get(url, params=None, headers=None, **kw):
        outcome = outcomes[len(calls)]
        calls.append(params)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    inner = MagicMock()
    inner.get = AsyncMock(side_effect=fake_get)

    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=inner)
    ctx.__aexit__ = AsyncMock(return_value=None)

    factory = MagicMock(return_value=ctx)
    return patch("app.integrations.dojah.client.httpx.AsyncClient", factory), calls


@pytest.mark.asyncio
async def test_4xx_does_not_retry(monkeypatch):
    client = _client(monkeypatch)
    patcher, calls = _patch_async_client([_mock_response(400, {"error": "bad reference"})])
    with patcher:
        with pytest.raises(httpx.HTTPStatusError):
            await client.fetch_verification(reference_id="KYC-BVN-1")
    assert len(calls) == 1  # no retry on a 4xx


@pytest.mark.asyncio
async def test_4xx_401_does_not_retry(monkeypatch):
    """A distinct 4xx (auth failure) — guards against a narrower-than-
    intended predicate that only special-cases 400."""
    client = _client(monkeypatch)
    patcher, calls = _patch_async_client([_mock_response(401, {"error": "unauthorized"})])
    with patcher:
        with pytest.raises(httpx.HTTPStatusError):
            await client.fetch_verification(reference_id="KYC-BVN-1")
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_5xx_retries_then_succeeds(monkeypatch):
    client = _client(monkeypatch)
    patcher, calls = _patch_async_client([
        _mock_response(503, {"error": "upstream unavailable"}),
        _mock_response(200, _success_payload()),
    ])
    with patcher:
        result = await client.fetch_verification(reference_id="KYC-BVN-1")
    assert result.status == "success"
    assert len(calls) == 2  # one retry after the 5xx


@pytest.mark.asyncio
async def test_5xx_exhausts_retries_and_raises(monkeypatch):
    client = _client(monkeypatch)
    patcher, calls = _patch_async_client([
        _mock_response(500, {"error": "1"}),
        _mock_response(500, {"error": "2"}),
        _mock_response(500, {"error": "3"}),
    ])
    with patcher:
        with pytest.raises(httpx.HTTPStatusError):
            await client.fetch_verification(reference_id="KYC-BVN-1")
    assert len(calls) == 3  # stop_after_attempt(3) exhausted


@pytest.mark.asyncio
async def test_transport_error_retries_then_succeeds(monkeypatch):
    client = _client(monkeypatch)
    patcher, calls = _patch_async_client([
        httpx.ConnectTimeout("connection timed out"),
        _mock_response(200, _success_payload()),
    ])
    with patcher:
        result = await client.fetch_verification(reference_id="KYC-BVN-1")
    assert result.status == "success"
    assert len(calls) == 2
