"""Real VTPassClient — cable TV paths. Same httpx mocking pattern as
`test_vtpass_client_electricity.py`; see that file for rationale."""
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.core.config import settings
from app.integrations.vtpass.client import VTPassClient
from app.integrations.vtpass.schemas import (
    BillDeliveryStatus,
    CablePlanList,
    SmartcardValidation,
)


@pytest.fixture
def vtpass_client(monkeypatch):
    monkeypatch.setattr(settings, "VTPASS_API_KEY", "test-api-key")
    monkeypatch.setattr(settings, "VTPASS_SECRET_KEY", "test-secret-key")
    monkeypatch.setattr(settings, "VTPASS_PUBLIC_KEY", "test-public-key")
    monkeypatch.setattr(settings, "VTPASS_BASE_URL", "https://sandbox.vtpass.test")
    return VTPassClient()


def _mock_httpx_response(
    *, status_code: int = 200, json_body: dict | None = None
) -> MagicMock:
    r = MagicMock(spec=httpx.Response)
    r.status_code = status_code
    if json_body is not None:
        r.json = MagicMock(return_value=json_body)
        r.text = ""
    else:
        r.json = MagicMock(side_effect=ValueError("no json"))
        r.text = ""
    return r


def _patch_async_client(*, post_return=None, get_return=None):
    inner = MagicMock()
    inner.post = AsyncMock(return_value=post_return)
    inner.get = AsyncMock(return_value=get_return)
    return patch("app.integrations.vtpass.client._http", return_value=inner), inner


# ── validate_smartcard ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_validate_smartcard_happy_path(vtpass_client):
    body = {
        "code": "000",
        "response_description": "VERIFICATION SUCCESSFUL",
        "content": {
            "Customer_Name":        "AYO SUBSCRIBER",
            "Current_Bouquet":      "DStv Compact",
            "Current_Bouquet_Code": "dstv-compact",
            "Status":               "Active",
            "Renewal_Amount":       "15500.00",
        },
    }
    patcher, inner = _patch_async_client(
        post_return=_mock_httpx_response(json_body=body)
    )
    with patcher:
        result = await vtpass_client.validate_smartcard(
            request_id="TMP-260421-10",
            service_id="dstv",
            smartcard_number="7031234567",
        )
    assert isinstance(result, SmartcardValidation)
    assert result.customer_name == "AYO SUBSCRIBER"
    assert result.current_plan_name == "DStv Compact"
    assert result.current_plan_code == "dstv-compact"
    assert result.status == "active"
    assert result.renewal_amount_ngn == Decimal("15500.00")
    # Wire body has no `type` — cable has no prepaid/postpaid axis.
    call = inner.post.call_args
    assert call.args[0].endswith("/api/merchant-verify")
    assert call.kwargs["json"] == {
        "billersCode": "7031234567",
        "serviceID":   "dstv",
    }


@pytest.mark.asyncio
async def test_validate_smartcard_inactive_returns_validation_not_raises(vtpass_client):
    """A code-000 response with Status="Inactive" is a *successful*
    lookup — the card is real, it's just not currently subscribed. The
    client must NOT raise; BillService will read `.status` and decide
    whether to surface a warning in the UI."""
    body = {
        "code": "000",
        "response_description": "VERIFICATION SUCCESSFUL",
        "content": {
            "Customer_Name":        "FRESH CARD",
            "Current_Bouquet":      "",
            "Current_Bouquet_Code": "",
            "Status":               "Inactive",
            "Renewal_Amount":       "0",
        },
    }
    patcher, _ = _patch_async_client(
        post_return=_mock_httpx_response(json_body=body)
    )
    with patcher:
        result = await vtpass_client.validate_smartcard(
            request_id="TMP-260421-11",
            service_id="dstv",
            smartcard_number="7039999999",
        )
    assert isinstance(result, SmartcardValidation)
    assert result.status == "inactive"
    assert result.current_plan_name == ""
    assert result.renewal_amount_ngn == Decimal("0.00")


# ── list_cable_plans ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_cable_plans_parses_variations_with_decimal_prices(vtpass_client):
    """`variation_amount` arrives as a string on the wire — must round-
    trip through _safe_decimal to Decimal so BillService price-matching
    is exact (no float drift)."""
    body = {
        "code": "000",
        "content": {
            "variations": [
                {
                    "variation_code":   "dstv-compact",
                    "name":             "DStv Compact",
                    "variation_amount": "15500.00",
                    "fixedPrice":       "Yes",
                },
                {
                    "variation_code":   "dstv-premium",
                    "name":             "DStv Premium",
                    "variation_amount": "44500.00",
                    "fixedPrice":       "Yes",
                },
            ],
        },
    }
    patcher, inner = _patch_async_client(
        get_return=_mock_httpx_response(json_body=body)
    )
    with patcher:
        result = await vtpass_client.list_cable_plans(service_id="dstv")
    assert isinstance(result, CablePlanList)
    assert result.service_id == "dstv"
    assert len(result.variations) == 2
    assert result.variations[0].variation_code == "dstv-compact"
    assert result.variations[0].price_ngn == Decimal("15500.00")
    assert result.variations[1].price_ngn == Decimal("44500.00")
    # GET to service-variations, not merchant-verify — catalog reads are
    # public-key, not secret-key (the client's `_get` enforces that).
    call = inner.get.call_args
    assert call.args[0].endswith("/api/service-variations")
    assert call.kwargs["params"] == {"serviceID": "dstv"}


@pytest.mark.asyncio
async def test_list_cable_plans_unknown_provider_returns_empty(vtpass_client):
    """VTPass returns a 200 with no `variations` array for unknown
    serviceIDs (rather than 404). The client must translate that to an
    empty list, not crash."""
    body = {
        "code": "000",
        "content": {},
    }
    patcher, _ = _patch_async_client(
        get_return=_mock_httpx_response(json_body=body)
    )
    with patcher:
        result = await vtpass_client.list_cable_plans(service_id="nosuch-tv")
    assert result.variations == []


# ── purchase_cable: wire-shape snapshots ──────────────────────────────
#
# The six tests below pin the wire body for every (provider × mode)
# combination so a regression to the prior shape (variation_code on
# renew, amount on change, subscription_type/quantity sent to StarTimes)
# fails loudly. Sources:
#   * https://vtpass.com/documentation/dstv-subscription-api/
#   * https://vtpass.com/documentation/gotv-subscription-api/
#   * https://vtpass.com/documentation/startimes-subscription-api/

_OK_RENEW_BODY = {
    "code": "000",
    "response_description": "TRANSACTION SUCCESSFUL",
    "amount": "15500",
    "content": {
        "transactions": {
            "status":        "delivered",
            "amount":        "15500",
            "transactionId": "vt_tx_cable_1",
        },
    },
}

_OK_CHANGE_BODY = {
    "code": "000",
    "response_description": "TRANSACTION SUCCESSFUL",
    "amount": "25000",
    "content": {
        "transactions": {
            "status":        "delivered",
            "amount":        "25000",
            "transactionId": "vt_tx_cable_2",
        },
    },
}


@pytest.mark.asyncio
async def test_purchase_cable_dstv_renew_wire_shape(vtpass_client):
    """DSTV renew per docs: subscription_type=renew + amount mandatory.
    variation_code is OMITTED — VTPass derives the bouquet from the
    smartcard's current subscription. quantity defaults to 1.
    serviceID stays as the bare slug (`dstv`, not `dstv-change`)."""
    patcher, inner = _patch_async_client(
        post_return=_mock_httpx_response(json_body=_OK_RENEW_BODY)
    )
    with patcher:
        result = await vtpass_client.purchase_cable(
            request_id="TMP-260421-20",
            service_id="dstv",
            smartcard_number="7031234567",
            variation_code="dstv-compact",
            amount_ngn=Decimal("15500.00"),
            subscription_type="renew",
            phone="08011111111",
        )
    assert result.status == BillDeliveryStatus.delivered
    assert result.delivered_amount_ngn == Decimal("15500.00")
    assert result.transaction_id == "vt_tx_cable_1"
    call = inner.post.call_args
    assert call.args[0].endswith("/api/pay")
    assert call.kwargs["json"] == {
        "request_id":        "TMP-260421-20",
        "serviceID":         "dstv",
        "billersCode":       "7031234567",
        "phone":             "08011111111",
        "subscription_type": "renew",
        "quantity":          1,
        "amount":            15500,
    }
    assert "variation_code" not in call.kwargs["json"]


@pytest.mark.asyncio
async def test_purchase_cable_dstv_change_wire_shape(vtpass_client):
    """DSTV change per docs: subscription_type=change + variation_code
    mandatory. amount is OMITTED — VTPass uses the price set for the
    bouquet (avoids stale-price drift between catalog GET and pay POST).
    serviceID stays as `dstv` — the prior `-change` suffix was wrong."""
    patcher, inner = _patch_async_client(
        post_return=_mock_httpx_response(json_body=_OK_CHANGE_BODY)
    )
    with patcher:
        result = await vtpass_client.purchase_cable(
            request_id="TMP-260421-21",
            service_id="dstv",
            smartcard_number="7031234567",
            variation_code="dstv-compact-plus",
            amount_ngn=Decimal("25000.00"),
            subscription_type="change",
            phone="08011111111",
        )
    assert result.status == BillDeliveryStatus.delivered
    call = inner.post.call_args
    assert call.kwargs["json"] == {
        "request_id":        "TMP-260421-21",
        "serviceID":         "dstv",
        "billersCode":       "7031234567",
        "phone":             "08011111111",
        "subscription_type": "change",
        "quantity":          1,
        "variation_code":    "dstv-compact-plus",
    }
    assert "amount" not in call.kwargs["json"]


@pytest.mark.asyncio
async def test_purchase_cable_gotv_renew_wire_shape(vtpass_client):
    """GOtv shares DSTV's docs shape — renew omits variation_code,
    sends amount + subscription_type."""
    patcher, inner = _patch_async_client(
        post_return=_mock_httpx_response(json_body=_OK_RENEW_BODY)
    )
    with patcher:
        await vtpass_client.purchase_cable(
            request_id="TMP-260421-30",
            service_id="gotv",
            smartcard_number="2025123456",
            variation_code="gotv-jolli",
            amount_ngn=Decimal("5700.00"),
            subscription_type="renew",
            phone="08022222222",
        )
    payload = inner.post.call_args.kwargs["json"]
    assert payload == {
        "request_id":        "TMP-260421-30",
        "serviceID":         "gotv",
        "billersCode":       "2025123456",
        "phone":             "08022222222",
        "subscription_type": "renew",
        "quantity":          1,
        "amount":            5700,
    }
    assert "variation_code" not in payload


@pytest.mark.asyncio
async def test_purchase_cable_gotv_change_wire_shape(vtpass_client):
    """GOtv change carries variation_code; amount is omitted."""
    patcher, inner = _patch_async_client(
        post_return=_mock_httpx_response(json_body=_OK_CHANGE_BODY)
    )
    with patcher:
        await vtpass_client.purchase_cable(
            request_id="TMP-260421-31",
            service_id="gotv",
            smartcard_number="2025123456",
            variation_code="gotv-max",
            amount_ngn=Decimal("8500.00"),
            subscription_type="change",
            phone="08022222222",
        )
    payload = inner.post.call_args.kwargs["json"]
    assert payload == {
        "request_id":        "TMP-260421-31",
        "serviceID":         "gotv",
        "billersCode":       "2025123456",
        "phone":             "08022222222",
        "subscription_type": "change",
        "quantity":          1,
        "variation_code":    "gotv-max",
    }
    assert "amount" not in payload


@pytest.mark.asyncio
async def test_purchase_cable_startimes_renew_wire_shape(vtpass_client):
    """StarTimes' docs differ from DSTV/GOtv: NO subscription_type,
    NO quantity. Every purchase is a flat variation_code charge — the
    caller represents 'renew' by re-paying the current variation.
    The client must drop both fields to stay strict to the docs."""
    patcher, inner = _patch_async_client(
        post_return=_mock_httpx_response(json_body=_OK_RENEW_BODY)
    )
    with patcher:
        await vtpass_client.purchase_cable(
            request_id="TMP-260421-40",
            service_id="startimes",
            smartcard_number="3033333333",
            variation_code="startimes-basic-monthly",
            amount_ngn=Decimal("2600.00"),
            subscription_type="renew",  # ignored on the wire
            phone="08033333333",
        )
    payload = inner.post.call_args.kwargs["json"]
    assert payload == {
        "request_id":     "TMP-260421-40",
        "serviceID":      "startimes",
        "billersCode":    "3033333333",
        "variation_code": "startimes-basic-monthly",
        "amount":         2600,
        "phone":          "08033333333",
    }
    assert "subscription_type" not in payload
    assert "quantity" not in payload


@pytest.mark.asyncio
async def test_purchase_cable_startimes_change_wire_shape(vtpass_client):
    """StarTimes change uses the same flat shape; the only difference
    is the variation_code. subscription_type and quantity are still
    omitted on the wire."""
    patcher, inner = _patch_async_client(
        post_return=_mock_httpx_response(json_body=_OK_CHANGE_BODY)
    )
    with patcher:
        await vtpass_client.purchase_cable(
            request_id="TMP-260421-41",
            service_id="startimes",
            smartcard_number="3033333333",
            variation_code="startimes-smart-monthly",
            amount_ngn=Decimal("3800.00"),
            subscription_type="change",  # ignored on the wire
            phone="08033333333",
        )
    payload = inner.post.call_args.kwargs["json"]
    assert payload == {
        "request_id":     "TMP-260421-41",
        "serviceID":      "startimes",
        "billersCode":    "3033333333",
        "variation_code": "startimes-smart-monthly",
        "amount":         3800,
        "phone":          "08033333333",
    }
    assert "subscription_type" not in payload
    assert "quantity" not in payload
