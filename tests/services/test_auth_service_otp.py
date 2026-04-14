import pytest
from datetime import datetime, timedelta

from app.services.auth_service import AuthService
from app.schemas.auth import RegisterRequest, VerifyOtpRequest
from app.integrations.termii.fake import FakeTermiiClient
from app.db.models.user import User, KycLevel
from app.db.models.otp import OtpCode, OtpPurpose
from app.core.security import hash_pin


async def _register(svc, phone="+2348011111111"):
    req = RegisterRequest(
        full_name="Test User",
        phone=phone,
        email=f"user_{phone[-4:]}@test.co",
        password="Secret1!",
    )
    return await svc.register(req)


@pytest.mark.asyncio
async def test_verify_otp_happy_path(db_session):
    sms = FakeTermiiClient()
    svc = AuthService(db=db_session, sms=sms)
    await _register(svc)

    # Retrieve the code from the fake SMS
    code = sms.sent[0].code_or_message
    req = VerifyOtpRequest(phone="+2348011111111", code=code)
    res = await svc.verify_otp(req)

    assert res.tokens.access_token
    assert res.tokens.refresh_token
    assert res.pin_set is False

    # KYC upgraded and phone verified
    user = db_session.query(User).filter_by(phone="+2348011111111").one()
    assert user.kyc_level == KycLevel.tier_1
    assert user.is_phone_verified is True


@pytest.mark.asyncio
async def test_verify_otp_wrong_code_increments_attempts(db_session):
    sms = FakeTermiiClient()
    svc = AuthService(db=db_session, sms=sms)
    await _register(svc)

    req = VerifyOtpRequest(phone="+2348011111111", code="000000")
    with pytest.raises(ValueError, match="INVALID_OTP"):
        await svc.verify_otp(req)

    otp = db_session.query(OtpCode).filter_by(phone="+2348011111111").one()
    assert otp.attempts == 1


@pytest.mark.asyncio
async def test_verify_otp_exceeds_attempts(db_session):
    sms = FakeTermiiClient()
    svc = AuthService(db=db_session, sms=sms)
    await _register(svc)

    # Manually set attempts to 3
    otp = db_session.query(OtpCode).filter_by(phone="+2348011111111").one()
    otp.attempts = 3
    db_session.commit()

    req = VerifyOtpRequest(phone="+2348011111111", code="000000")
    with pytest.raises(ValueError, match="OTP_ATTEMPTS_EXCEEDED"):
        await svc.verify_otp(req)


@pytest.mark.asyncio
async def test_verify_otp_expired(db_session):
    sms = FakeTermiiClient()
    svc = AuthService(db=db_session, sms=sms)
    await _register(svc)

    # Manually expire the OTP
    otp = db_session.query(OtpCode).filter_by(phone="+2348011111111").one()
    otp.expires_at = datetime.utcnow() - timedelta(minutes=1)
    db_session.commit()

    code = sms.sent[0].code_or_message
    req = VerifyOtpRequest(phone="+2348011111111", code=code)
    with pytest.raises(ValueError, match="OTP_EXPIRED"):
        await svc.verify_otp(req)


@pytest.mark.asyncio
async def test_verify_otp_user_not_found(db_session):
    sms = FakeTermiiClient()
    svc = AuthService(db=db_session, sms=sms)

    req = VerifyOtpRequest(phone="+2348099999999", code="123456")
    with pytest.raises(ValueError, match="USER_NOT_FOUND"):
        await svc.verify_otp(req)
