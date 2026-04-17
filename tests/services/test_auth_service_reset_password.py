import pytest

from app.services.auth_service import AuthService
from app.schemas.auth import RegisterRequest, LoginRequest, VerifyEmailOtpRequest
from app.integrations.termii.fake import FakeTermiiClient
from app.integrations.email.fake import FakeEmailClient
from app.services.token_store import NullTokenStore


async def _register(svc, sms, em, phone="+2348011111111"):
    email = f"reset_{phone[-4:]}@test.co"
    req = RegisterRequest(full_name="Reset User", phone=phone, email=email, password="Secret1!")
    await svc.register(req)
    code = em.sent[-1].code_or_body
    await svc.verify_email_otp(VerifyEmailOtpRequest(email=email, code=code))


@pytest.mark.asyncio
async def test_reset_password_happy_path(db_session):
    sms = FakeTermiiClient()
    em = FakeEmailClient()
    svc = AuthService(db=db_session, sms=sms, email=em, token_store=NullTokenStore())
    await _register(svc, sms, em)
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
    em = FakeEmailClient()
    svc = AuthService(db=db_session, sms=sms, email=em, token_store=NullTokenStore())
    await _register(svc, sms, em)

    await svc.forgot_password("+2348011111111")

    with pytest.raises(ValueError, match="INVALID_OTP"):
        await svc.reset_password("+2348011111111", "000000", "NewSecret1!")


@pytest.mark.asyncio
async def test_reset_password_user_not_found(db_session):
    sms = FakeTermiiClient()
    em = FakeEmailClient()
    svc = AuthService(db=db_session, sms=sms, email=em, token_store=NullTokenStore())

    with pytest.raises(ValueError, match="USER_NOT_FOUND"):
        await svc.reset_password("ghost@test.co", "123456", "NewSecret1!")
