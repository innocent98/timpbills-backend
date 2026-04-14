import pytest

from app.services.auth_service import AuthService
from app.schemas.auth import RegisterRequest, VerifyOtpRequest
from app.integrations.termii.fake import FakeTermiiClient
from app.db.models.otp import OtpCode, OtpPurpose


async def _register(svc, sms, phone="+2348011111111"):
    req = RegisterRequest(full_name="Forgot User", phone=phone, email=f"forgot_{phone[-4:]}@test.co", password="Secret1!")
    await svc.register(req)
    code = sms.sent[-1].code_or_message
    await svc.verify_otp(VerifyOtpRequest(phone=phone, code=code))


@pytest.mark.asyncio
async def test_forgot_password_sends_otp(db_session):
    sms = FakeTermiiClient()
    svc = AuthService(db=db_session, sms=sms)
    await _register(svc, sms)
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
    svc = AuthService(db=db_session, sms=sms)

    # Should not raise, should not send SMS
    await svc.forgot_password("ghost@test.co")
    assert len(sms.sent) == 0
