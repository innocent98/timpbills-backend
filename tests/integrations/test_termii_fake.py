import pytest
from app.integrations.termii.fake import FakeTermiiClient


@pytest.mark.asyncio
async def test_records_sent_otps():
    fake = FakeTermiiClient()
    await fake.send_otp(phone="+2348011111111", code="123456")
    assert len(fake.sent) == 1
    assert fake.sent[0].code_or_message == "123456"
