import pytest

from app.services.auth_service import AuthService
from app.schemas.auth import RegisterRequest, LoginRequest, VerifyOtpRequest
from app.integrations.termii.fake import FakeTermiiClient


async def _register_and_verify(svc, sms, phone="+2348011111111", email="user@test.co"):
    req = RegisterRequest(full_name="Login User", phone=phone, email=email, password="Secret1!")
    await svc.register(req)
    code = sms.sent[-1].code_or_message
    await svc.verify_otp(VerifyOtpRequest(phone=phone, code=code))


@pytest.mark.asyncio
async def test_login_with_email(db_session):
    sms = FakeTermiiClient()
    svc = AuthService(db=db_session, sms=sms)
    await _register_and_verify(svc, sms)

    res = await svc.login(LoginRequest(identifier="user@test.co", password="Secret1!"))
    assert res.tokens.access_token
    assert res.tokens.refresh_token
    assert res.pin_set is False


@pytest.mark.asyncio
async def test_login_with_phone(db_session):
    sms = FakeTermiiClient()
    svc = AuthService(db=db_session, sms=sms)
    await _register_and_verify(svc, sms)

    res = await svc.login(LoginRequest(identifier="+2348011111111", password="Secret1!"))
    assert res.tokens.access_token


@pytest.mark.asyncio
async def test_login_wrong_password(db_session):
    sms = FakeTermiiClient()
    svc = AuthService(db=db_session, sms=sms)
    await _register_and_verify(svc, sms)

    with pytest.raises(ValueError, match="INVALID_CREDENTIALS"):
        await svc.login(LoginRequest(identifier="user@test.co", password="WrongPass1!"))


@pytest.mark.asyncio
async def test_login_unknown_user(db_session):
    sms = FakeTermiiClient()
    svc = AuthService(db=db_session, sms=sms)

    with pytest.raises(ValueError, match="INVALID_CREDENTIALS"):
        await svc.login(LoginRequest(identifier="ghost@test.co", password="Secret1!"))
