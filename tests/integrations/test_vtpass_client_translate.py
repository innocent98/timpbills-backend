"""Pure-function tests for translate_response — the envelope-to-
normalized-schema mapper shared by VTPassClient and /webhooks/vtpass.
The real HTTP calls are exercised against the VTPass sandbox during
sprint closure (V2); here we only verify that whatever JSON VTPass
produces is interpreted correctly."""
from decimal import Decimal

from app.integrations.vtpass.client import _safe_decimal, translate_response
from app.integrations.vtpass.schemas import BillDeliveryStatus


def _trans(body: dict, request_id="TMP-X", requested=Decimal("500.00")):
    return translate_response(body, request_id=request_id, requested=requested)


def test_translate_success_envelope():
    body = {
        "code": "000",
        "response_description": "TRANSACTION SUCCESSFUL",
        "content": {
            "transactions": {
                "status": "delivered",
                "amount": "500",
                "transactionId": "vt_123",
            }
        },
    }
    r = _trans(body)
    assert r.status == BillDeliveryStatus.delivered
    assert r.code == "000"
    assert r.transaction_id == "vt_123"
    assert r.delivered_amount_ngn == Decimal("500.00")
    assert r.requested_amount_ngn == Decimal("500.00")


def test_translate_partial_delivery():
    body = {
        "code": "000",
        "response_description": "PARTIAL DELIVERY",
        "content": {
            "transactions": {
                "status": "delivered",
                "amount": "450",
                "transactionId": "vt_123",
            }
        },
    }
    r = _trans(body)
    assert r.status == BillDeliveryStatus.delivered
    assert r.requested_amount_ngn == Decimal("500.00")
    assert r.delivered_amount_ngn == Decimal("450.00")


def test_translate_pending_envelope():
    body = {
        "code": "099",
        "response_description": "Pending upstream confirmation",
        "content": {},
    }
    r = _trans(body)
    assert r.status == BillDeliveryStatus.pending
    assert r.delivered_amount_ngn == Decimal("0.00")


def test_translate_failed_envelope():
    body = {
        "code": "016",
        "response_description": "TRANSACTION FAILED",
        "content": {},
    }
    r = _trans(body)
    assert r.status == BillDeliveryStatus.failed
    assert r.delivered_amount_ngn == Decimal("0.00")
    assert r.raw == body


def test_translate_missing_content_transactions():
    """VTPass sometimes drops content.transactions on hard failures.
    Our code must not NoneType-crash."""
    body = {
        "code": "016",
        "response_description": "BAD AMOUNT",
        "content": None,
    }
    r = _trans(body)
    assert r.status == BillDeliveryStatus.failed
    assert r.transaction_id == ""


def test_safe_decimal_handles_strings_and_nones():
    assert _safe_decimal("500") == Decimal("500.00")
    assert _safe_decimal(None) == Decimal("0.00")
    assert _safe_decimal("not a number") == Decimal("0.00")
    assert _safe_decimal(500) == Decimal("500.00")
    assert _safe_decimal(Decimal("500.00")) == Decimal("500.00")
