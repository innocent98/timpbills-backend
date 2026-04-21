"""FakeVTPassClient contract + test hooks."""
from decimal import Decimal

import pytest

from app.integrations.vtpass.fake import FakeVTPassClient
from app.integrations.vtpass.schemas import BillDeliveryStatus


@pytest.mark.asyncio
async def test_purchase_airtime_defaults_to_success():
    fake = FakeVTPassClient()
    r = await fake.purchase_airtime(
        request_id="TMP-TEST-1", service_id="mtn", phone="08012345678",
        amount_ngn=Decimal("500.00"),
    )
    assert r.status == BillDeliveryStatus.delivered
    assert r.requested_amount_ngn == Decimal("500.00")
    assert r.delivered_amount_ngn == Decimal("500.00")
    assert r.code == "000"
    assert r.transaction_id.startswith("vtp_")


@pytest.mark.asyncio
async def test_will_fail_returns_failed():
    fake = FakeVTPassClient()
    fake.will_fail("TMP-TEST-2")
    r = await fake.purchase_airtime(
        request_id="TMP-TEST-2", service_id="mtn", phone="08012345678",
        amount_ngn=Decimal("500.00"),
    )
    assert r.status == BillDeliveryStatus.failed
    assert r.delivered_amount_ngn == Decimal("0.00")


@pytest.mark.asyncio
async def test_will_remain_pending_returns_pending():
    fake = FakeVTPassClient()
    fake.will_remain_pending("TMP-TEST-3")
    r = await fake.purchase_airtime(
        request_id="TMP-TEST-3", service_id="mtn", phone="08012345678",
        amount_ngn=Decimal("500.00"),
    )
    assert r.status == BillDeliveryStatus.pending
    assert r.code == "099"


@pytest.mark.asyncio
async def test_will_partial_delivers_less_than_requested():
    fake = FakeVTPassClient()
    fake.will_partial("TMP-TEST-4", delivered_ngn=Decimal("450.00"))
    r = await fake.purchase_airtime(
        request_id="TMP-TEST-4", service_id="mtn", phone="08012345678",
        amount_ngn=Decimal("500.00"),
    )
    assert r.status == BillDeliveryStatus.delivered
    assert r.requested_amount_ngn == Decimal("500.00")
    assert r.delivered_amount_ngn == Decimal("450.00")
    assert r.raw["partial"] is True


@pytest.mark.asyncio
async def test_will_partial_default_delta_is_5():
    """When no explicit delivered_ngn is given, the fake delivers
    requested - 5 so tests can assert "partial" without caring about
    exact values."""
    fake = FakeVTPassClient()
    fake.will_partial("TMP-TEST-5")
    r = await fake.purchase_airtime(
        request_id="TMP-TEST-5", service_id="mtn", phone="08012345678",
        amount_ngn=Decimal("100.00"),
    )
    assert r.delivered_amount_ngn == Decimal("95.00")


@pytest.mark.asyncio
async def test_list_data_plans_returns_seeded_catalog():
    fake = FakeVTPassClient()
    plans = await fake.list_data_plans(service_id="mtn-data")
    assert plans.service_id == "mtn-data"
    assert len(plans.variations) >= 3
    # At least one variation has a positive price.
    assert all(v.price_ngn > 0 for v in plans.variations)


@pytest.mark.asyncio
async def test_list_data_plans_unknown_service_returns_empty():
    fake = FakeVTPassClient()
    plans = await fake.list_data_plans(service_id="bogus-data")
    assert plans.variations == []


@pytest.mark.asyncio
async def test_purchase_data_looks_up_price_from_catalog():
    fake = FakeVTPassClient()
    r = await fake.purchase_data(
        request_id="TMP-TEST-6", service_id="mtn-data",
        phone="08012345678", variation_code="mtn-1gb-monthly",
    )
    assert r.status == BillDeliveryStatus.delivered
    assert r.requested_amount_ngn == Decimal("1000.00")
    assert r.delivered_amount_ngn == Decimal("1000.00")


@pytest.mark.asyncio
async def test_purchase_data_unknown_variation_fails():
    """Prevents a client-spoofed variation_code from succeeding silently.
    The fake surfaces the unknown plan as a failed purchase so BillService
    can exercise its refund path."""
    fake = FakeVTPassClient()
    r = await fake.purchase_data(
        request_id="TMP-TEST-7", service_id="mtn-data",
        phone="08012345678", variation_code="bogus-plan",
    )
    assert r.status == BillDeliveryStatus.failed


@pytest.mark.asyncio
async def test_requery_returns_same_status_and_amount():
    fake = FakeVTPassClient()
    fake.will_remain_pending("TMP-TEST-8")
    await fake.purchase_airtime(
        request_id="TMP-TEST-8", service_id="mtn", phone="08012345678",
        amount_ngn=Decimal("200.00"),
    )
    r = await fake.requery(request_id="TMP-TEST-8")
    assert r.status == BillDeliveryStatus.pending
    assert r.requested_amount_ngn == Decimal("200.00")


@pytest.mark.asyncio
async def test_fake_is_billprovider_protocol_compliant():
    from app.integrations.vtpass.base import BillProvider
    fake = FakeVTPassClient()
    # runtime_checkable Protocol — isinstance catches missing methods.
    assert isinstance(fake, BillProvider)
