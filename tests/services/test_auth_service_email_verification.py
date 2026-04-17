"""Tests for the email OTP verification flow (new primary register→verify gate)."""
import pytest
from datetime import datetime, timedelta

from app.services.auth_service import AuthService
from app.schemas.auth import RegisterRequest, VerifyEmailOtpRequest
from app.integrations.termii.fake import FakeTermiiClient
from app.integrations.email.fake import FakeEmailClient
from app.db.models.user import User, KycLevel
from app.db.models.otp import OtpCode, OtpPurpose
from app.services.token_store import NullTokenStore


async def _register(svc: AuthService, email_client: FakeEmailClient, phone="+2348011111111", email="user@test.co"):
    req = RegisterRequest(
        full_name="Test User",
        phone=phone,
        email=email,
        password="Secret1!",
    )
    return await svc.register(req)


@pytest.mark.asyncio
async def test_verify_email_otp_happy_path(db_session):
    sms = FakeTermiiClient()
    email = FakeEmailClient()
    svc = AuthService(db=db_session, sms=sms, email=email, token_store=NullTokenStore())
    await _register(svc, email)

    code = email.sent[0].code_or_body
    req = VerifyEmailOtpRequest(email="user@test.co", code=code)
    res = await svc.verify_email_otp(req)

    assert res.tokens.access_token
    assert res.tokens.refresh_token
    assert res.pin_set is False
    assert res.phone_verified is False

    user = db_session.query(User).filter_by(email="user@test.co").one()
    assert user.email_verified is True
    # kyc_level stays tier_0 after email verification (phone upgrade needed for tier_1)
    assert user.kyc_level == KycLevel.tier_0
    assert user.is_phone_verified is False


@pytest.mark.asyncio
async def test_verify_email_otp_wrong_code_increments_attempts(db_session):
    sms = FakeTermiiClient()
    email = FakeEmailClient()
    svc = AuthService(db=db_session, sms=sms, email=email, token_store=NullTokenStore())
    await _register(svc, email)

    req = VerifyEmailOtpRequest(email="user@test.co", code="000000")
    with pytest.raises(ValueError, match="INVALID_OTP"):
        await svc.verify_email_otp(req)

    otp = (
        db_session.query(OtpCode)
        .filter_by(purpose=OtpPurpose.email_verification)
        .one()
    )
    assert otp.attempts == 1


@pytest.mark.asyncio
async def test_verify_email_otp_exceeds_attempts(db_session):
    sms = FakeTermiiClient()
    email = FakeEmailClient()
    svc = AuthService(db=db_session, sms=sms, email=email, token_store=NullTokenStore())
    await _register(svc, email)

    otp = (
        db_session.query(OtpCode)
        .filter_by(purpose=OtpPurpose.email_verification)
        .one()
    )
    otp.attempts = 3
    db_session.commit()

    req = VerifyEmailOtpRequest(email="user@test.co", code="000000")
    with pytest.raises(ValueError, match="OTP_ATTEMPTS_EXCEEDED"):
        await svc.verify_email_otp(req)


@pytest.mark.asyncio
async def test_verify_email_otp_expired(db_session):
    sms = FakeTermiiClient()
    email = FakeEmailClient()
    svc = AuthService(db=db_session, sms=sms, email=email, token_store=NullTokenStore())
    await _register(svc, email)

    otp = (
        db_session.query(OtpCode)
        .filter_by(purpose=OtpPurpose.email_verification)
        .one()
    )
    otp.expires_at = datetime.utcnow() - timedelta(minutes=1)
    db_session.commit()

    code = email.sent[0].code_or_body
    req = VerifyEmailOtpRequest(email="user@test.co", code=code)
    with pytest.raises(ValueError, match="OTP_EXPIRED"):
        await svc.verify_email_otp(req)


@pytest.mark.asyncio
async def test_verify_email_otp_user_not_found(db_session):
    sms = FakeTermiiClient()
    email = FakeEmailClient()
    svc = AuthService(db=db_session, sms=sms, email=email, token_store=NullTokenStore())

    req = VerifyEmailOtpRequest(email="ghost@test.co", code="123456")
    with pytest.raises(ValueError, match="USER_NOT_FOUND"):
        await svc.verify_email_otp(req)


@pytest.mark.asyncio
async def test_send_email_otp_resend(db_session):
    sms = FakeTermiiClient()
    email = FakeEmailClient()
    svc = AuthService(db=db_session, sms=sms, email=email, token_store=NullTokenStore())
    await _register(svc, email)

    assert len(email.sent) == 1
    await svc.send_email_otp("user@test.co")
    assert len(email.sent) == 2
    # Both sent to the same address
    assert email.sent[1].to == "user@test.co"


@pytest.mark.asyncio
async def test_send_email_otp_already_verified_raises(db_session):
    sms = FakeTermiiClient()
    email = FakeEmailClient()
    svc = AuthService(db=db_session, sms=sms, email=email, token_store=NullTokenStore())
    await _register(svc, email)

    code = email.sent[0].code_or_body
    await svc.verify_email_otp(VerifyEmailOtpRequest(email="user@test.co", code=code))

    with pytest.raises(ValueError, match="EMAIL_ALREADY_VERIFIED"):
        await svc.send_email_otp("user@test.co")
