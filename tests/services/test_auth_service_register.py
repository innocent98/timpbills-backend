import pytest
from app.services.auth_service import AuthService
from app.schemas.auth import RegisterRequest
from app.integrations.termii.fake import FakeTermiiClient
from app.db.models.user import User


@pytest.mark.asyncio
async def test_register_creates_user_and_sends_otp(db_session):
    sms = FakeTermiiClient()
    svc = AuthService(db=db_session, sms=sms)
    req = RegisterRequest(full_name="Test User", phone="+2348011111111", email="t@t.co", password="Secret1!")
    res = await svc.register(req)
    assert res.phone == "+2348011111111"
    assert db_session.query(User).filter_by(phone="+2348011111111").one()
    assert len(sms.sent) == 1
    assert len(sms.sent[0].code_or_message) == 6


@pytest.mark.asyncio
async def test_register_rejects_duplicate_phone(db_session):
    sms = FakeTermiiClient()
    svc = AuthService(db=db_session, sms=sms)
    req = RegisterRequest(full_name="Alice", phone="+2348011111111", email="a@a.co", password="Secret1!")
    await svc.register(req)
    req2 = req.model_copy(update={"email": "b@b.co"})
    with pytest.raises(Exception):
        await svc.register(req2)
