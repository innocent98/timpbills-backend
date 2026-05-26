"""Tests for phone OTP verification (on-demand Tier 1 upgrade)."""
import pytest
from uuid import uuid4

from app.services.auth_service import AuthService
from app.schemas.auth import RegisterRequest, VerifyEmailOtpRequest
from app.integrations.termii.fake import FakeTermiiClient
from app.integrations.email.fake import FakeEmailClient
from app.db.models.user import User, KycLevel
from app.db.models.otp import OtpCode, OtpPurpose
from app.services.token_store import NullTokenStore


async def _register_and_verify_email(db_session, phone="+2348011111111", email="user@test.co"):
    """Helper: register user and verify email to get tokens."""
    sms = FakeTermiiClient()
    em = FakeEmailClient()
    svc = AuthService(db=db_session, sms=sms, email=em, token_store=NullTokenStore())
    req = RegisterRequest(full_name="Phone User", phone=phone, email=email, password="Secret1!")
    await svc.register(req)
    code = em.sent[0].code_or_body
    await svc.verify_email_otp(VerifyEmailOtpRequest(email=email, code=code))
    return svc, sms, em


@pytest.mark.asyncio
async def test_send_phone_otp_happy_path(db_session):
    """B8 update: register now sends a phone OTP too, so the explicit
    send_phone_otp call below is the second SMS, not the first."""
    svc, sms, em = await _register_and_verify_email(db_session)
    user = db_session.query(User).filter_by(phone="+2348011111111").one()
    register_sms_count = len(sms.sent)

    await svc.send_phone_otp(user_id=user.id)

    assert len(sms.sent) == register_sms_count + 1
    assert sms.sent[-1].phone == "+2348011111111"
    assert len(sms.sent[-1].code_or_message) == 6


@pytest.mark.asyncio
async def test_verify_phone_otp_upgrades_tier(db_session):
    svc, sms, em = await _register_and_verify_email(db_session)
    user = db_session.query(User).filter_by(phone="+2348011111111").one()

    await svc.send_phone_otp(user_id=user.id)
    # The newest OTP — the one just emitted by send_phone_otp — is what
    # verify_phone_otp will consume (it orders by created_at DESC).
    code = sms.sent[-1].code_or_message

    res = await svc.verify_phone_otp(user_id=user.id, code=code)

    assert res.tokens.access_token
    assert res.tokens.refresh_token
    assert res.pin_set is False

    db_session.refresh(user)
    assert user.is_phone_verified is True
    assert user.kyc_level == KycLevel.tier_1


@pytest.mark.asyncio
async def test_verify_phone_otp_wrong_code(db_session):
    """B8 update: register now persists a phone_verification OTP too, so
    the table holds *two* rows after send_phone_otp. The attempts counter
    we care about is on the newest row (the one verify_phone_otp picked)."""
    svc, sms, em = await _register_and_verify_email(db_session)
    user = db_session.query(User).filter_by(phone="+2348011111111").one()

    await svc.send_phone_otp(user_id=user.id)

    with pytest.raises(ValueError, match="INVALID_OTP"):
        await svc.verify_phone_otp(user_id=user.id, code="000000")

    latest = (
        db_session.query(OtpCode)
        .filter_by(purpose=OtpPurpose.phone_verification)
        .order_by(OtpCode.created_at.desc())
        .first()
    )
    assert latest is not None
    assert latest.attempts == 1


@pytest.mark.asyncio
async def test_send_phone_otp_already_verified(db_session):
    svc, sms, em = await _register_and_verify_email(db_session)
    user = db_session.query(User).filter_by(phone="+2348011111111").one()

    await svc.send_phone_otp(user_id=user.id)
    # Newest OTP — same reason as test_verify_phone_otp_upgrades_tier.
    code = sms.sent[-1].code_or_message
    await svc.verify_phone_otp(user_id=user.id, code=code)

    with pytest.raises(ValueError, match="PHONE_ALREADY_VERIFIED"):
        await svc.send_phone_otp(user_id=user.id)


@pytest.mark.asyncio
async def test_send_phone_otp_user_not_found(db_session):
    svc, sms, em = await _register_and_verify_email(db_session)

    with pytest.raises(ValueError, match="USER_NOT_FOUND"):
        await svc.send_phone_otp(user_id=uuid4())
