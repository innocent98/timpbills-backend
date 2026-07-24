"""Real PaystackClient — DVA calls: 4xx handling and retry policy.

Regression: a Paystack rejection (e.g. "wema-bank is not available in test
mode", HTTP 400) escaped `assign_dedicated_account` as a raw
`httpx.HTTPStatusError` via `raise_for_status`, which the wallet endpoint
did not catch -- so the app saw a bare 500 instead of a readable message.
Worse, the retry decorator retried the 400 three times first, spending
~10s of backoff on a request that could never succeed.

A 4xx from Paystack must become a `PaystackError` (carrying Paystack's own
message) on the FIRST attempt, so the endpoint maps it to 502. Only 5xx
and transport errors are retried. Same fail-fast policy the Dojah client
already uses -- see tests/integrations/dojah/test_client_retry.py.

Patches `app.integrations.paystack.client.httpx.AsyncClient` with a
context-manager stub, exercising the real tenacity-decorated methods
without a network call.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.core.config import settings
from app.integrations.paystack.client import PaystackClient
from app.integrations.paystack.errors import PaystackError


def _client(monkeypatch) -> PaystackClient:
    monkeypatch.setattr(settings, "PAYSTACK_SECRET_KEY", "sk_test_x")
    monkeypatch.setattr(settings, "PAYSTACK_BASE_URL", "https://paystack.test")
    return PaystackClient()


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
    """`outcomes` is consumed in order, one per POST call — mirrors tenacity's
    successive retry attempts. A BaseException instance is raised instead of
    returned (simulates a transport error)."""
    calls: list = []

    async def fake_post(url, json=None, headers=None, **kw):
        outcome = outcomes[len(calls)]
        calls.append(json)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    inner = MagicMock()
    inner.post = AsyncMock(side_effect=fake_post)

    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=inner)
    ctx.__aexit__ = AsyncMock(return_value=None)

    factory = MagicMock(return_value=ctx)
    return patch("app.integrations.paystack.client.httpx.AsyncClient", factory), calls


async def _assign(client: PaystackClient):
    return await client.assign_dedicated_account(
        email="u@example.com", first_name="Uchenna", middle_name="",
        last_name="Okoro", phone="+2348100000000", preferred_bank="wema-bank",
        country="NG", account_number="0111111111", bvn="22222222221",
        bank_code="070",
    )


@pytest.mark.asyncio
async def test_assign_4xx_becomes_paystack_error_without_retry(monkeypatch):
    client = _client(monkeypatch)
    patcher, calls = _patch_async_client([
        _mock_response(400, {"status": False,
                             "message": "wema-bank is not available in test mode"}),
    ])
    with patcher:
        with pytest.raises(PaystackError, match="wema-bank is not available"):
            await _assign(client)
    assert len(calls) == 1  # a 400 will never succeed — no wasted retries


@pytest.mark.asyncio
async def test_assign_5xx_retries_then_succeeds(monkeypatch):
    client = _client(monkeypatch)
    patcher, calls = _patch_async_client([
        _mock_response(503, {"status": False, "message": "upstream"}),
        _mock_response(200, {"status": True,
                             "message": "Assign dedicated account in progress"}),
    ])
    with patcher:
        result = await _assign(client)
    assert result.status is True
    assert len(calls) == 2  # one retry after the 5xx


@pytest.mark.asyncio
async def test_create_customer_4xx_becomes_paystack_error_without_retry(monkeypatch):
    client = _client(monkeypatch)
    patcher, calls = _patch_async_client([
        _mock_response(400, {"status": False, "message": "email is invalid"}),
    ])
    with patcher:
        with pytest.raises(PaystackError, match="email is invalid"):
            await client.create_customer(
                email="bad", first_name="U", last_name="O", phone="+234",
            )
    assert len(calls) == 1
