import pytest
from app.integrations.email.fake import FakeEmailClient


@pytest.mark.asyncio
async def test_send_otp_records_email():
    fake = FakeEmailClient()
    await fake.send_otp(to="user@example.com", code="123456")
    assert len(fake.sent) == 1
    assert fake.sent[0].to == "user@example.com"
    assert fake.sent[0].code_or_body == "123456"
    assert "verification code" in fake.sent[0].subject.lower()


@pytest.mark.asyncio
async def test_send_text_records_email():
    fake = FakeEmailClient()
    await fake.send_text(to="user@example.com", subject="Hello", html="<p>Hi</p>", text="Hi")
    assert len(fake.sent) == 1
    assert fake.sent[0].subject == "Hello"
    assert fake.sent[0].code_or_body == "Hi"


@pytest.mark.asyncio
async def test_send_text_falls_back_to_html():
    fake = FakeEmailClient()
    await fake.send_text(to="user@example.com", subject="Hello", html="<p>Hi</p>")
    assert fake.sent[0].code_or_body == "<p>Hi</p>"


@pytest.mark.asyncio
async def test_multiple_sends_accumulate():
    fake = FakeEmailClient()
    await fake.send_otp(to="a@a.com", code="111111")
    await fake.send_otp(to="b@b.com", code="222222")
    assert len(fake.sent) == 2
    assert fake.sent[1].to == "b@b.com"
