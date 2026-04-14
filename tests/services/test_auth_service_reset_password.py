import pytest

from app.services.auth_service import AuthService
from app.schemas.auth import RegisterRequest, LoginRequest, VerifyOtpRequest
from app.integrations.termii.fake import FakeTermiiClient


async def _register(svc, sms, phone="+2348011111111"):
    req = RegisterRequest(full_name="Reset User", phone=phone, email=f"reset_{phone[-4:]}@test.co", password="Secret1!")
    await svc.register(req)
    code = sms.sent[-1].code_or_message
    await svc.verify_otp(VerifyOtpRequest(phone=phone, code=code))


@pytest.mark.asyncio
async def test_reset_password_happy_path(db_session):
    sms = FakeTermiiClient()
    svc = AuthService(db=db_session, sms=sms)
    await _register(svc, sms)
    sms.sent.clear()

    await svc.forgot_password("+2348011111111")
    reset_code = sms.sent[-1].code_or_message

    await svc.reset_password("+2348011111111", reset_code, "NewSecret1!")

    # Should be able to login with new password
    res = await svc.login(LoginRequest(identifier="+2348011111111", password="NewSecret1!"))
    assert res.tokens.access_token

    # Old password should no longer work
    with pytest.raises(ValueError, match="INVALID_CREDENTIALS"):
        await svc.login(LoginRequest(identifier="+2348011111111", password="Secret1!"))


@pytest.mark.asyncio
async def test_reset_password_wrong_code(db_session):
    sms = FakeTermiiClient()
    svc = AuthService(db=db_session, sms=sms)
    await _register(svc, sms)

    await svc.forgot_password("+2348011111111")

    with pytest.raises(ValueError, match="INVALID_OTP"):
        await svc.reset_password("+2348011111111", "000000", "NewSecret1!")


@pytest.mark.asyncio
async def test_reset_password_user_not_found(db_session):
    sms = FakeTermiiClient()
    svc = AuthService(db=db_session, sms=sms)

    with pytest.raises(ValueError, match="USER_NOT_FOUND"):
        await svc.reset_password("ghost@test.co", "123456", "NewSecret1!")
