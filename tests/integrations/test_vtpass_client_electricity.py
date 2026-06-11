"""Real VTPassClient — electricity paths. We mock `httpx.AsyncClient` at
the module level so the client thinks it's talking to VTPass but we
control the response envelope, mirroring the pattern in
`test_vtpass_client_translate.py` (which only covers the pure mapper).

The client's auth-header plumbing and retry decorator are exercised
implicitly; we assert on the observable result (typed schema) and on
the error translation (5xx → temporary, 4xx → permanent, non-000 →
permanent for validation reads).
"""
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.core.config import settings
from app.integrations.vtpass.base import (
    ProviderPermanentFailure,
    ProviderTemporaryFailure,
)
from app.integrations.vtpass.client import VTPassClient
from app.integrations.vtpass.schemas import BillDeliveryStatus, MeterValidation


@pytest.fixture
def vtpass_client(monkeypatch):
    """Build a real VTPassClient with fabricated credentials so the
    __init__ guard passes. Never calls the network — callers patch
    `httpx.AsyncClient` to stub responses."""
    monkeypatch.setattr(settings, "VTPASS_API_KEY", "test-api-key")
    monkeypatch.setattr(settings, "VTPASS_SECRET_KEY", "test-secret-key")
    monkeypatch.setattr(settings, "VTPASS_PUBLIC_KEY", "test-public-key")
    monkeypatch.setattr(settings, "VTPASS_BASE_URL", "https://sandbox.vtpass.test")
    return VTPassClient()


def _mock_httpx_response(
    *,
    status_code: int = 200,
    json_body: dict | None = None,
    text_body: str = "",
) -> MagicMock:
    """Build a MagicMock that quacks like an httpx.Response."""
    r = MagicMock(spec=httpx.Response)
    r.status_code = status_code
    if json_body is not None:
        r.json = MagicMock(return_value=json_body)
        r.text = ""
    else:
        r.json = MagicMock(side_effect=ValueError("no json"))
        r.text = text_body
    return r


def _patch_async_client(*, post_return=None, get_return=None, post_side_effect=None):
    """Patch the shared httpx client (vtpass.client._http) so .post/.get
    return our mocked response(s)."""
    inner = MagicMock()
    if post_side_effect is not None:
        inner.post = AsyncMock(side_effect=post_side_effect)
    else:
        inner.post = AsyncMock(return_value=post_return)
    inner.get = AsyncMock(return_value=get_return)
    return patch("app.integrations.vtpass.client._http", return_value=inner), inner


# ── validate_meter ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_validate_meter_happy_path(vtpass_client):
    """Code 000 + populated content → MeterValidation with customer
    fields echoed out of content, meter_type round-tripped from input."""
    body = {
        "code": "000",
        "response_description": "VERIFICATION SUCCESSFUL",
        "content": {
            "Customer_Name":  "JANE DOE",
            "Address":        "12 FAKE ST, IKEJA",
            "Meter_Number":   "1234567890",
            "Meter_Type":     "PREPAID",
        },
    }
    patcher, inner = _patch_async_client(
        post_return=_mock_httpx_response(json_body=body)
    )
    with patcher:
        result = await vtpass_client.validate_meter(
            request_id="TMP-260421-1",
            service_id="ikeja-electric",
            meter_number="1234567890",
            meter_type="prepaid",
        )
    assert isinstance(result, MeterValidation)
    assert result.customer_name == "JANE DOE"
    assert result.address == "12 FAKE ST, IKEJA"
    assert result.meter_number == "1234567890"
    assert result.meter_type == "prepaid"
    # Assert the wire call went to merchant-verify with the expected body.
    call = inner.post.call_args
    assert call.args[0].endswith("/api/merchant-verify")
    assert call.kwargs["json"] == {
        "billersCode": "1234567890",
        "serviceID":   "ikeja-electric",
        "type":        "prepaid",
    }


@pytest.mark.asyncio
async def test_validate_meter_non_000_raises_permanent(vtpass_client):
    """Invalid meter number is deterministic — must raise
    ProviderPermanentFailure so BillService returns 422, never pending."""
    body = {
        "code": "013",
        "response_description": "INVALID METER NUMBER",
        "content": {},
    }
    patcher, _ = _patch_async_client(
        post_return=_mock_httpx_response(json_body=body)
    )
    with patcher:
        with pytest.raises(ProviderPermanentFailure) as excinfo:
            await vtpass_client.validate_meter(
                request_id="TMP-260421-2",
                service_id="ikeja-electric",
                meter_number="0000000000",
                meter_type="prepaid",
            )
    assert "013" in str(excinfo.value)


# ── purchase_electricity ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_purchase_electricity_returns_token_and_units_in_raw(vtpass_client):
    """On success, `content.transactions.token` and `.units` must
    surface on the returned BillPurchaseResponse.raw so BillService can
    persist them on the Transaction row."""
    body = {
        "code": "000",
        "response_description": "TRANSACTION SUCCESSFUL",
        "amount": "5000",
        "content": {
            "transactions": {
                "status":        "delivered",
                "amount":        "5000",
                "transactionId": "vt_tx_elec_1",
                "token":         "1234-5678-9012-3456-7890",
                "units":         "125.00",
            },
        },
    }
    patcher, inner = _patch_async_client(
        post_return=_mock_httpx_response(json_body=body)
    )
    with patcher:
        result = await vtpass_client.purchase_electricity(
            request_id="TMP-260421-3",
            service_id="ikeja-electric",
            meter_number="1234567890",
            meter_type="prepaid",
            amount_ngn=Decimal("5000.00"),
            phone="08012345678",
        )
    assert result.status == BillDeliveryStatus.delivered
    assert result.delivered_amount_ngn == Decimal("5000.00")
    assert result.raw["token"] == "1234-5678-9012-3456-7890"
    assert result.raw["units"] == "125.00"
    # Wire payload: variation_code carries meter_type; phone is passed.
    call = inner.post.call_args
    assert call.args[0].endswith("/api/pay")
    assert call.kwargs["json"]["variation_code"] == "prepaid"
    assert call.kwargs["json"]["phone"] == "08012345678"
    assert call.kwargs["json"]["billersCode"] == "1234567890"


@pytest.mark.asyncio
async def test_purchase_electricity_pending_099(vtpass_client):
    """Code 099 → pending, delivered_amount=0. The reconcile worker
    will requery later."""
    body = {
        "code": "099",
        "response_description": "Pending upstream confirmation",
        "content": {},
    }
    patcher, _ = _patch_async_client(
        post_return=_mock_httpx_response(json_body=body)
    )
    with patcher:
        result = await vtpass_client.purchase_electricity(
            request_id="TMP-260421-4",
            service_id="ikeja-electric",
            meter_number="1234567890",
            meter_type="prepaid",
            amount_ngn=Decimal("2000.00"),
            phone="08012345678",
        )
    assert result.status == BillDeliveryStatus.pending
    assert result.code == "099"
    assert result.delivered_amount_ngn == Decimal("0.00")


@pytest.mark.asyncio
async def test_purchase_electricity_5xx_raises_temporary(vtpass_client):
    """5xx → ProviderTemporaryFailure. The @retry on `_post_pay` will
    retry up to 3x internally; we assert the final re-raised exception."""
    patcher, _ = _patch_async_client(
        post_return=_mock_httpx_response(
            status_code=503, text_body="Service Unavailable"
        )
    )
    with patcher:
        with pytest.raises(ProviderTemporaryFailure):
            await vtpass_client.purchase_electricity(
                request_id="TMP-260421-5",
                service_id="ikeja-electric",
                meter_number="1234567890",
                meter_type="prepaid",
                amount_ngn=Decimal("1000.00"),
                phone="08012345678",
            )


@pytest.mark.asyncio
async def test_purchase_electricity_4xx_raises_permanent(vtpass_client):
    """4xx → ProviderPermanentFailure. No retry, no pending."""
    patcher, _ = _patch_async_client(
        post_return=_mock_httpx_response(
            status_code=400, text_body="Bad Request"
        )
    )
    with patcher:
        with pytest.raises(ProviderPermanentFailure):
            await vtpass_client.purchase_electricity(
                request_id="TMP-260421-6",
                service_id="ikeja-electric",
                meter_number="1234567890",
                meter_type="prepaid",
                amount_ngn=Decimal("1000.00"),
                phone="08012345678",
            )
