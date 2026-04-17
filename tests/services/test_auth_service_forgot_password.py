import pytest

from app.services.auth_service import AuthService
from app.schemas.auth import RegisterRequest, VerifyEmailOtpRequest
from app.integrations.termii.fake import FakeTermiiClient
from app.integrations.email.fake import FakeEmailClient
from app.db.models.otp import OtpCode, OtpPurpose
from app.services.token_store import NullTokenStore


async def _register(svc, sms, em, phone="+2348011111111"):
    email = f"forgot_{phone[-4:]}@test.co"
    req = RegisterRequest(full_name="Forgot User", phone=phone, email=email, password="Secret1!")
    await svc.register(req)
    code = em.sent[-1].code_or_body
    await svc.verify_email_otp(VerifyEmailOtpRequest(email=email, code=code))


@pytest.mark.asyncio
async def test_forgot_password_sends_otp(db_session):
    sms = FakeTermiiClient()
    em = FakeEmailClient()
    svc = AuthService(db=db_session, sms=sms, email=em, token_store=NullTokenStore())
    await _register(svc, sms, em)
    sms.sent.clear()  # clear previous messages

    await svc.forgot_password("+2348011111111")

    assert len(sms.sent) == 1
    otp = (
        db_session.query(OtpCode)
        .filter_by(phone="+2348011111111", purpose=OtpPurpose.password_reset)
        .one()
    )
    assert otp is not None


@pytest.mark.asyncio
async def test_forgot_password_silent_for_unknown(db_session):
    sms = FakeTermiiClient()
    em = FakeEmailClient()
    svc = AuthService(db=db_session, sms=sms, email=em, token_store=NullTokenStore())

    # Should not raise, should not send SMS
    await svc.forgot_password("ghost@test.co")
    assert len(sms.sent) == 0
