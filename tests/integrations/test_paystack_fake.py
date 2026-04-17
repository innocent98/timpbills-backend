import pytest

from app.integrations.paystack.fake import FakePaystackClient


@pytest.mark.asyncio
async def test_fake_initialize_returns_auth_url():
    c = FakePaystackClient()
    res = await c.initialize(
        amount_kobo=500000, email="a@b.co", reference="TMP-REF-1"
    )
    assert res.authorization_url.endswith("TMP-REF-1")
    assert c.initialized == [("TMP-REF-1", 500000)]


@pytest.mark.asyncio
async def test_fake_verify_returns_configured_outcome():
    c = FakePaystackClient()
    await c.initialize(amount_kobo=500000, email="a@b.co", reference="TMP-REF-2")
    c.will_succeed("TMP-REF-2")
    v = await c.verify(reference="TMP-REF-2")
    assert v.status == "success"


def test_fake_signature_accepts_FAKE_SIG():
    c = FakePaystackClient()
    assert c.verify_signature(raw_body=b"{}", signature="FAKE_SIG") is True
    assert c.verify_signature(raw_body=b"{}", signature="nope") is False
