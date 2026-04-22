"""Schema construction + field-validation tests for the Sprint 4
cable/electricity additions to `app/integrations/vtpass/schemas.py`.

These mirror the patterns used by the existing `DataPlanVariation` /
`DataPlanList` / `BillPurchaseResponse` tests: we construct with valid
data, confirm frozenness, and exercise the `ge=0` guards on money
fields (S3C-M5 convention).

Protocol method additions in `base.py` are behavior-free in this
ticket (B1 is protocol-only; Fake + real client are B2/B3), so there
are no protocol tests here."""
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.integrations.vtpass.schemas import (
    CablePlanList,
    CablePlanVariation,
    MeterValidation,
    SmartcardValidation,
)


def test_meter_validation_constructs_and_is_frozen():
    m = MeterValidation(
        service_id="ikeja-electric",
        meter_number="1234567890",
        customer_name="Jane Doe",
        address="12 Allen Ave, Ikeja",
        meter_type="prepaid",
    )
    assert m.service_id == "ikeja-electric"
    assert m.meter_number == "1234567890"
    assert m.customer_name == "Jane Doe"
    assert m.address == "12 Allen Ave, Ikeja"
    assert m.meter_type == "prepaid"

    # frozen=True — mutation must raise.
    with pytest.raises(ValidationError):
        m.customer_name = "Someone Else"  # type: ignore[misc]


def test_smartcard_validation_accepts_empty_plan_fields():
    """A brand-new smartcard can have no current plan attached; the
    schema must not force those two strings to be non-empty."""
    s = SmartcardValidation(
        service_id="dstv",
        smartcard_number="7011234567",
        customer_name="Jane Doe",
        current_plan_name="",
        current_plan_code="",
        status="active",
        renewal_amount_ngn=Decimal("0"),
    )
    assert s.current_plan_name == ""
    assert s.current_plan_code == ""
    assert s.status == "active"
    assert s.renewal_amount_ngn == Decimal("0")


def test_smartcard_validation_rejects_negative_renewal_amount():
    with pytest.raises(ValidationError):
        SmartcardValidation(
            service_id="dstv",
            smartcard_number="7011234567",
            customer_name="Jane Doe",
            current_plan_name="Compact",
            current_plan_code="dstv-compact",
            status="active",
            renewal_amount_ngn=Decimal("-1"),
        )


def test_cable_plan_variation_valid_and_rejects_negative_price():
    v = CablePlanVariation(
        variation_code="dstv-compact",
        name="DStv Compact",
        price_ngn=Decimal("15700.00"),
        validity="1 month",
    )
    assert v.variation_code == "dstv-compact"
    assert v.price_ngn == Decimal("15700.00")
    assert v.validity == "1 month"

    with pytest.raises(ValidationError):
        CablePlanVariation(
            variation_code="dstv-compact",
            name="DStv Compact",
            price_ngn=Decimal("-1"),
        )


def test_cable_plan_list_allows_empty_variations():
    """An empty catalog is a valid (if unusual) response — e.g. during
    temporary provider maintenance. Downstream code should treat it as
    "no plans available," not a schema error."""
    lst = CablePlanList(service_id="dstv", variations=[])
    assert lst.service_id == "dstv"
    assert lst.variations == []


def test_cable_plan_list_serializes_prices_as_decimal():
    """Guard against accidental float coercion in pydantic v2 serialization
    — prices must round-trip as Decimal so downstream money math stays
    exact."""
    lst = CablePlanList(
        service_id="dstv",
        variations=[
            CablePlanVariation(
                variation_code="dstv-compact",
                name="DStv Compact",
                price_ngn=Decimal("15700.00"),
                validity="1 month",
            ),
        ],
    )
    dumped = lst.model_dump()
    assert isinstance(dumped["variations"][0]["price_ngn"], Decimal)
    assert dumped["variations"][0]["price_ngn"] == Decimal("15700.00")
