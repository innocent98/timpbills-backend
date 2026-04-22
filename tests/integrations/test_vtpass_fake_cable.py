"""FakeVTPassClient cable extensions — Sprint 4 B2."""
from decimal import Decimal

import pytest

from app.integrations.vtpass.base import ProviderPermanentFailure
from app.integrations.vtpass.fake import FakeVTPassClient
from app.integrations.vtpass.schemas import (
    BillDeliveryStatus,
    CablePlanList,
    SmartcardValidation,
)


@pytest.mark.asyncio
async def test_list_cable_plans_dstv_returns_seeded_catalog():
    fake = FakeVTPassClient()
    plans = await fake.list_cable_plans(service_id="dstv")
    assert isinstance(plans, CablePlanList)
    assert plans.service_id == "dstv"
    assert len(plans.variations) == 4
    names = {v.name for v in plans.variations}
    assert {"Compact", "Compact Plus", "Premium", "Access"} == names
    # All prices positive (and specifically > 0 Decimal).
    assert all(v.price_ngn > 0 for v in plans.variations)


@pytest.mark.asyncio
async def test_list_cable_plans_unknown_service_returns_empty():
    """Unknown service_id is not an exception — mirrors list_data_plans'
    contract so callers can branch on an empty list rather than
    try/except."""
    fake = FakeVTPassClient()
    plans = await fake.list_cable_plans(service_id="unknown-cable")
    assert isinstance(plans, CablePlanList)
    assert plans.service_id == "unknown-cable"
    assert plans.variations == []


@pytest.mark.asyncio
async def test_validate_smartcard_returns_default_active_compact_plan():
    fake = FakeVTPassClient()
    result = await fake.validate_smartcard(
        request_id="TMP-CABLE-1",
        service_id="dstv",
        smartcard_number="1234567890",
    )
    assert isinstance(result, SmartcardValidation)
    assert result.service_id == "dstv"
    assert result.smartcard_number == "1234567890"
    assert result.status == "active"
    assert result.current_plan_name == "Fake Compact Plan"
    assert result.renewal_amount_ngn == Decimal("5000.00")
    assert result.customer_name != ""


@pytest.mark.asyncio
async def test_validate_smartcard_will_invalid_smartcard_raises():
    fake = FakeVTPassClient()
    fake.will_invalid_smartcard(service_id="dstv", smartcard_number="0000000000")
    with pytest.raises(ProviderPermanentFailure):
        await fake.validate_smartcard(
            request_id="TMP-CABLE-2",
            service_id="dstv",
            smartcard_number="0000000000",
        )


@pytest.mark.asyncio
async def test_purchase_cable_happy_path_uses_catalog_price():
    """variation_code looks up the seed catalog so the price is
    authoritative — client can't spoof it."""
    fake = FakeVTPassClient()
    plans = await fake.list_cable_plans(service_id="dstv")
    compact = next(v for v in plans.variations if v.name == "Compact")
    r = await fake.purchase_cable(
        request_id="TMP-CABLE-3",
        service_id="dstv",
        smartcard_number="1234567890",
        variation_code=compact.variation_code,
        amount_ngn=compact.price_ngn,
    )
    assert r.status == BillDeliveryStatus.delivered
    assert r.requested_amount_ngn == compact.price_ngn
    assert r.delivered_amount_ngn == compact.price_ngn


@pytest.mark.asyncio
async def test_purchase_cable_unknown_variation_fails():
    """Matches purchase_data fake's behavior (fake.py:95-97) — unknown
    variation_code surfaces as a failed purchase so BillService can
    exercise its refund path."""
    fake = FakeVTPassClient()
    r = await fake.purchase_cable(
        request_id="TMP-CABLE-4",
        service_id="dstv",
        smartcard_number="1234567890",
        variation_code="bogus-bouquet",
        amount_ngn=Decimal("1000.00"),
    )
    assert r.status == BillDeliveryStatus.failed
