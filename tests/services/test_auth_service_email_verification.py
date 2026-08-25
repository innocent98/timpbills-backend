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
    """B9: a fresh registration's /verify_email_otp returns the
    ``phone_verification_required`` branch — email_verified flips but
    no tokens are issued yet (phone gate not passed).

    The phone OTP is now sent lazily at THIS step (the phone gate just
    became active), so the SMS fake captures exactly one send,
    ``phone_otp_sent`` is True, and a phone_verification OtpCode row
    exists. Register itself sent no SMS (see test_auth_service_register).
    """
    sms = FakeTermiiClient()
    email = FakeEmailClient()
    svc = AuthService(db=db_session, sms=sms, email=email, token_store=NullTokenStore())
    await _register(svc, email)

    # Register sent no SMS — verify the baseline before the verify step.
    assert len(sms.sent) == 0

    code = email.sent[0].code_or_body
    req = VerifyEmailOtpRequest(email="user@test.co", code=code)
    res = await svc.verify_email_otp(req)

    assert res.email_verified is True
    assert res.phone_verified is False
    assert res.pin_set is False
    assert res.next_action == "phone_verification_required"
    assert res.tokens is None
    assert res.pin_setup_token is None
    assert res.phone_otp_sent is True

    # The phone OTP went out exactly once, to the user's phone.
    assert len(sms.sent) == 1
    assert sms.sent[0].phone == "+2348011111111"
    assert len(sms.sent[0].code_or_message) == 6

    user = db_session.query(User).filter_by(email="user@test.co").one()
    assert user.email_verified is True
    assert user.kyc_level == KycLevel.tier_0
    assert user.is_phone_verified is False

    # A phone_verification OtpCode row now exists.
    phone_otp = (
        db_session.query(OtpCode)
        .filter_by(user_id=user.id, purpose=OtpPurpose.phone_verification)
        .one()
    )
    assert phone_otp.used_at is None


@pytest.mark.asyncio
async def test_verify_email_otp_cooldown_blocks_phone_send(db_session):
    """If a phone_verification OTP was minted within the cooldown window
    just before the email-verify step, the lazy phone send is swallowed:
    ``phone_otp_sent`` is False, no exception, no extra SMS."""
    from datetime import UTC, datetime, timedelta

    from app.core.security import hash_pin

    sms = FakeTermiiClient()
    email = FakeEmailClient()
    svc = AuthService(db=db_session, sms=sms, email=email, token_store=NullTokenStore())
    await _register(svc, email)

    user = db_session.query(User).filter_by(email="user@test.co").one()
    # Seed a *fresh* phone OTP so the cooldown helper trips.
    db_session.add(OtpCode(
        user_id=user.id, phone=user.phone,
        code_hash=hash_pin("000000"),
        purpose=OtpPurpose.phone_verification,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    ))
    db_session.commit()
    sms.sent.clear()

    code = email.sent[0].code_or_body
    res = await svc.verify_email_otp(VerifyEmailOtpRequest(email="user@test.co", code=code))

    assert res.next_action == "phone_verification_required"
    assert res.phone_otp_sent is False
    assert len(sms.sent) == 0


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
