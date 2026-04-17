import pytest
from app.services.auth_service import AuthService
from app.schemas.auth import RegisterRequest
from app.integrations.termii.fake import FakeTermiiClient
from app.integrations.email.fake import FakeEmailClient
from app.db.models.user import User
from app.services.token_store import NullTokenStore


@pytest.mark.asyncio
async def test_register_creates_user_and_sends_email_otp(db_session):
    sms = FakeTermiiClient()
    email = FakeEmailClient()
    svc = AuthService(db=db_session, sms=sms, email=email, token_store=NullTokenStore())
    req = RegisterRequest(full_name="Test User", phone="+2348011111111", email="t@t.co", password="Secret1!")
    res = await svc.register(req)

    assert res.phone == "+2348011111111"
    assert res.email == "t@t.co"
    assert db_session.query(User).filter_by(phone="+2348011111111").one()

    # Email OTP sent, NOT SMS
    assert len(email.sent) == 1
    assert len(email.sent[0].code_or_body) == 6
    assert email.sent[0].to == "t@t.co"
    assert len(sms.sent) == 0


@pytest.mark.asyncio
async def test_register_rejects_duplicate_phone(db_session):
    sms = FakeTermiiClient()
    email = FakeEmailClient()
    svc = AuthService(db=db_session, sms=sms, email=email, token_store=NullTokenStore())
    req = RegisterRequest(full_name="Alice", phone="+2348011111111", email="a@a.co", password="Secret1!")
    await svc.register(req)
    req2 = req.model_copy(update={"email": "b@b.co"})
    with pytest.raises(Exception):
        await svc.register(req2)


@pytest.mark.asyncio
async def test_register_rejects_duplicate_email(db_session):
    sms = FakeTermiiClient()
    email = FakeEmailClient()
    svc = AuthService(db=db_session, sms=sms, email=email, token_store=NullTokenStore())
    req = RegisterRequest(full_name="Bob", phone="+2348011111112", email="same@same.co", password="Secret1!")
    await svc.register(req)
    req2 = req.model_copy(update={"phone": "+2348011111113"})
    with pytest.raises(ValueError, match="USER_ALREADY_EXISTS"):
        await svc.register(req2)
