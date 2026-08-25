import pytest

from app.integrations.paystack.fake import (
    FakePaystackClient,
    customer_identification_event,
    dedicated_account_assign_event,
    dva_charge_event,
)


@pytest.mark.asyncio
async def test_create_customer_is_deterministic_by_email():
    fake = FakePaystackClient()
    a = await fake.create_customer(
        email="ada@x.co", first_name="Ada", last_name="Obi", phone="+2348000000001"
    )
    b = await fake.create_customer(
        email="ada@x.co", first_name="Ada", last_name="Obi", phone="+2348000000001"
    )
    assert a.customer_code == b.customer_code  # idempotent by email
    assert a.customer_code.startswith("CUS_")


@pytest.mark.asyncio
async def test_assign_records_call_and_returns_202_shape():
    fake = FakePaystackClient()
    res = await fake.assign_dedicated_account(
        email="ada@x.co", first_name="Ada", middle_name="Grace", last_name="Obi",
        phone="+2348000000001", preferred_bank="test-bank", country="NG",
        account_number="0123456789", bvn="22222222222", bank_code="035",
    )
    assert res.status is True
    assert fake.assigned == [("ada@x.co", "0123456789", "22222222222", "035")]


@pytest.mark.asyncio
async def test_list_banks_returns_items():
    fake = FakePaystackClient()
    banks = await fake.list_banks(country="nigeria")
    assert any(b.slug == "test-bank" for b in banks)
    assert all(b.code for b in banks)


def test_webhook_fixture_builders_shape():
    ci = customer_identification_event(customer_code="CUS_1", success=True)
    assert ci["event"] == "customeridentification.success"
    assert ci["data"]["customer_code"] == "CUS_1"

    da = dedicated_account_assign_event(
        customer_code="CUS_1", account_number="9988776655",
        account_name="ADA OBI", bank_name="Wema Bank", bank_slug="wema-bank",
    )
    assert da["event"] == "dedicatedaccount.assign.success"
    assert da["data"]["dedicated_account"]["account_number"] == "9988776655"

    ch = dva_charge_event(account_number="9988776655", amount_kobo=500000)
    assert ch["event"] == "charge.success"
    assert ch["data"]["channel"] == "dedicated_nuban"
    assert ch["data"]["authorization"]["receiver_bank_account_number"] == "9988776655"
