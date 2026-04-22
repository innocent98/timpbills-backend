"""FakeVTPassClient electricity extensions — Sprint 4 B2.

Mirrors the style of `test_vtpass_fake.py`: thin per-behavior assertions,
no BillService involvement (that lands in B3+)."""
from decimal import Decimal

import pytest

from app.integrations.vtpass.base import ProviderPermanentFailure
from app.integrations.vtpass.fake import FakeVTPassClient
from app.integrations.vtpass.schemas import BillDeliveryStatus, MeterValidation


@pytest.mark.asyncio
async def test_validate_meter_returns_deterministic_details():
    """Happy path: seeded DisCo + arbitrary meter number returns a
    populated MeterValidation. The fake fabricates a stable customer
    name + address so tests can assert without coupling to a real DisCo
    API response."""
    fake = FakeVTPassClient()
    result = await fake.validate_meter(
        request_id="TMP-ELEC-1",
        service_id="ikeja-electric",
        meter_number="1234567890",
        meter_type="prepaid",
    )
    assert isinstance(result, MeterValidation)
    assert result.service_id == "ikeja-electric"
    assert result.meter_number == "1234567890"
    assert result.meter_type == "prepaid"
    # Customer fields are non-empty — exact string is an implementation
    # detail but determinism matters, so two calls must agree.
    assert result.customer_name != ""
    assert result.address != ""
    again = await fake.validate_meter(
        request_id="TMP-ELEC-1",
        service_id="ikeja-electric",
        meter_number="1234567890",
        meter_type="prepaid",
    )
    assert again.customer_name == result.customer_name
    assert again.address == result.address


@pytest.mark.asyncio
async def test_validate_meter_will_reject_meter_raises():
    """Test hook forces an InvalidMeter failure. Per the plan, we use
    ProviderPermanentFailure rather than a new exception class — the
    validation vs purchase distinction is already carried by the method
    signature."""
    fake = FakeVTPassClient()
    fake.will_reject_meter(service_id="ikeja-electric", meter_number="0000000000")
    with pytest.raises(ProviderPermanentFailure):
        await fake.validate_meter(
            request_id="TMP-ELEC-2",
            service_id="ikeja-electric",
            meter_number="0000000000",
            meter_type="prepaid",
        )


@pytest.mark.asyncio
async def test_purchase_electricity_happy_path_returns_token_and_units():
    """Success path: 20-digit token + kWh units in raw. Token is
    deterministic from the request_id so replay/requery tests stay stable."""
    fake = FakeVTPassClient()
    r = await fake.purchase_electricity(
        request_id="TMP-ELEC-3",
        service_id="ikeja-electric",
        meter_number="1234567890",
        meter_type="prepaid",
        amount_ngn=Decimal("2000.00"),
    )
    assert r.status == BillDeliveryStatus.delivered
    assert r.requested_amount_ngn == Decimal("2000.00")
    assert r.delivered_amount_ngn == Decimal("2000.00")
    token = r.raw.get("token")
    assert isinstance(token, str)
    assert len(token) == 20
    assert token.isdigit()
    # Units populated as a decimal string like "50.00".
    assert "units" in r.raw
    assert Decimal(r.raw["units"]) > Decimal("0")


@pytest.mark.asyncio
async def test_purchase_electricity_will_fail_returns_failed_zero_delivered():
    fake = FakeVTPassClient()
    fake.will_fail("TMP-ELEC-4")
    r = await fake.purchase_electricity(
        request_id="TMP-ELEC-4",
        service_id="ikeja-electric",
        meter_number="1234567890",
        meter_type="prepaid",
        amount_ngn=Decimal("2000.00"),
    )
    assert r.status == BillDeliveryStatus.failed
    assert r.delivered_amount_ngn == Decimal("0.00")
    # Token injection only runs on delivered status — a failed purchase
    # must not leak a fabricated token into raw.
    assert "token" not in r.raw


@pytest.mark.asyncio
async def test_purchase_electricity_will_partial_honours_delivered_amount():
    fake = FakeVTPassClient()
    fake.will_partial("TMP-ELEC-5", delivered_ngn=Decimal("1500.00"))
    r = await fake.purchase_electricity(
        request_id="TMP-ELEC-5",
        service_id="ikeja-electric",
        meter_number="1234567890",
        meter_type="prepaid",
        amount_ngn=Decimal("2000.00"),
    )
    assert r.status == BillDeliveryStatus.delivered
    assert r.requested_amount_ngn == Decimal("2000.00")
    assert r.delivered_amount_ngn == Decimal("1500.00")


@pytest.mark.asyncio
async def test_purchase_electricity_will_remain_pending_returns_pending():
    fake = FakeVTPassClient()
    fake.will_remain_pending("TMP-ELEC-6")
    r = await fake.purchase_electricity(
        request_id="TMP-ELEC-6",
        service_id="ikeja-electric",
        meter_number="1234567890",
        meter_type="prepaid",
        amount_ngn=Decimal("2000.00"),
    )
    assert r.status == BillDeliveryStatus.pending
    assert r.delivered_amount_ngn == Decimal("0.00")
