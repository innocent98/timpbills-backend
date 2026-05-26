"""Service-layer tests for AuthService.login.

B12: login is now phone-only and returns a ``next_action`` discriminator
instead of always issuing tokens. Gate evaluation is email → phone →
pin; the first unverified gate decides the response shape. An
unverified-email login no longer raises ``EMAIL_NOT_VERIFIED`` — it
returns 200 with ``next_action=email_verification_required`` and no
tokens.
"""
import pytest

from app.db.models.user import User
from app.integrations.email.fake import FakeEmailClient
from app.integrations.termii.fake import FakeTermiiClient
from app.schemas.auth import LoginRequest, RegisterRequest, VerifyEmailOtpRequest
from app.services.auth_service import AuthService
from app.services.token_store import NullTokenStore


async def _register_and_verify(svc, email_client, phone="+2348011111111", email="user@test.co"):
    """Register + email-verify. To reach ``tokens_issued`` on login the
    caller must also pre-stamp the migration state (phone-verified + PIN);
    that happens inline in each test via ``_stamp_migration``."""
    req = RegisterRequest(full_name="Login User", phone=phone, email=email, password="Secret1!")
    await svc.register(req)
    code = email_client.sent[-1].code_or_body
    await svc.verify_email_otp(VerifyEmailOtpRequest(email=email, code=code))


def _stamp_migration(db, *, email, pin="8527"):
    """Force phone-verified + PIN so /login takes ``tokens_issued``."""
    from app.core.security import hash_pin
    user = db.query(User).filter(User.email == email).one()
    user.is_phone_verified = True
    user.pin_hash = hash_pin(pin)
    db.commit()


@pytest.mark.asyncio
async def test_login_with_phone_issues_tokens(db_session):
    """Happy path — all three gates pass → ``tokens_issued`` + token pair."""
    sms = FakeTermiiClient()
    em = FakeEmailClient()
    svc = AuthService(db=db_session, sms=sms, email=em, token_store=NullTokenStore())
    await _register_and_verify(svc, em)
    _stamp_migration(db_session, email="user@test.co")

    res = await svc.login(LoginRequest(phone="+2348011111111", password="Secret1!"))
    assert res.next_action == "tokens_issued"
    assert res.tokens is not None
    assert res.tokens.access_token
    assert res.tokens.refresh_token
    assert res.pin_set is True


@pytest.mark.asyncio
async def test_login_normalises_local_phone(db_session):
    """Local 080... format must resolve the +234 row via normalize_to_e164."""
    sms = FakeTermiiClient()
    em = FakeEmailClient()
    svc = AuthService(db=db_session, sms=sms, email=em, token_store=NullTokenStore())
    await _register_and_verify(svc, em)
    _stamp_migration(db_session, email="user@test.co")

    res = await svc.login(LoginRequest(phone="08011111111", password="Secret1!"))
    assert res.next_action == "tokens_issued"
    assert res.tokens.access_token


@pytest.mark.asyncio
async def test_login_wrong_password(db_session):
    sms = FakeTermiiClient()
    em = FakeEmailClient()
    svc = AuthService(db=db_session, sms=sms, email=em, token_store=NullTokenStore())
    await _register_and_verify(svc, em)

    with pytest.raises(ValueError, match="INVALID_CREDENTIALS"):
        await svc.login(LoginRequest(phone="+2348011111111", password="WrongPass1!"))


@pytest.mark.asyncio
async def test_login_unknown_user(db_session):
    sms = FakeTermiiClient()
    em = FakeEmailClient()
    svc = AuthService(db=db_session, sms=sms, email=em, token_store=NullTokenStore())

    with pytest.raises(ValueError, match="INVALID_CREDENTIALS"):
        await svc.login(LoginRequest(phone="+2348099999999", password="Secret1!"))


@pytest.mark.asyncio
async def test_login_email_field_in_phone_position_400s(db_session):
    """Phone-only field — an email payload in the ``phone`` slot must
    surface as INVALID_PHONE_FORMAT, not USER_NOT_FOUND."""
    sms = FakeTermiiClient()
    em = FakeEmailClient()
    svc = AuthService(db=db_session, sms=sms, email=em, token_store=NullTokenStore())

    with pytest.raises(ValueError, match="INVALID_PHONE_FORMAT"):
        await svc.login(LoginRequest(phone="ghost@test.co", password="Secret1!"))


@pytest.mark.asyncio
async def test_login_unverified_email_returns_email_action(db_session):
    """B12 contract: an unverified email no longer raises — it returns
    ``next_action=email_verification_required`` with no tokens. Routing
    is mobile's responsibility from here."""
    sms = FakeTermiiClient()
    em = FakeEmailClient()
    svc = AuthService(db=db_session, sms=sms, email=em, token_store=NullTokenStore())

    # Register but do NOT verify email
    req = RegisterRequest(
        full_name="Unverified User", phone="+2348011111112",
        email="unverified@test.co", password="Secret1!",
    )
    await svc.register(req)

    res = await svc.login(LoginRequest(phone="+2348011111112", password="Secret1!"))
    assert res.next_action == "email_verification_required"
    assert res.tokens is None


@pytest.mark.asyncio
async def test_login_rejects_inactive_user(db_session):
    sms = FakeTermiiClient()
    em = FakeEmailClient()
    svc = AuthService(db=db_session, sms=sms, email=em, token_store=NullTokenStore())

    # Register and fully verify
    await _register_and_verify(svc, em, phone="+2348011111113", email="inactive@test.co")
    _stamp_migration(db_session, email="inactive@test.co")

    # Disable the user directly in DB
    user = db_session.query(User).filter_by(email="inactive@test.co").one()
    user.is_active = False
    db_session.commit()

    with pytest.raises(ValueError, match="ACCOUNT_DISABLED"):
        await svc.login(LoginRequest(phone="+2348011111113", password="Secret1!"))


@pytest.mark.asyncio
async def test_login_phone_unverified_returns_phone_action_and_sends_otp(db_session):
    """Email verified, phone NOT verified, no PIN → ``next_action=
    phone_verification_required`` with an inline OTP dispatched.

    Register seeded a phone OTP — we backdate its created_at past the
    cooldown window so the inline-send path on login isn't blocked.
    """
    from datetime import UTC, datetime, timedelta

    from app.core.config import settings
    from app.db.models.otp import OtpCode

    sms = FakeTermiiClient()
    em = FakeEmailClient()
    svc = AuthService(db=db_session, sms=sms, email=em, token_store=NullTokenStore())
    await _register_and_verify(svc, em, phone="+2348011111114", email="phoneonly@test.co")
    # Backdate any existing phone-verification OTPs so cooldown doesn't fire.
    cooldown_back = timedelta(seconds=settings.OTP_RESEND_COOLDOWN_SECONDS + 10)
    db_session.query(OtpCode).update(
        {"created_at": datetime.now(UTC) - cooldown_back}
    )
    db_session.commit()
    sms.sent.clear()

    res = await svc.login(LoginRequest(phone="+2348011111114", password="Secret1!"))
    assert res.next_action == "phone_verification_required"
    assert res.tokens is None
    assert res.phone_otp_sent is True
    assert len(sms.sent) == 1


@pytest.mark.asyncio
async def test_login_no_pin_returns_pin_setup_token(db_session):
    """Email + phone verified, PIN unset (existing-user migration) →
    ``next_action=pin_setup_required`` with a scoped pin_setup token."""
    sms = FakeTermiiClient()
    em = FakeEmailClient()
    svc = AuthService(db=db_session, sms=sms, email=em, token_store=NullTokenStore())
    await _register_and_verify(svc, em, phone="+2348011111115", email="nopin@test.co")
    # Stamp phone-verified but leave pin_hash None
    user = db_session.query(User).filter_by(email="nopin@test.co").one()
    user.is_phone_verified = True
    db_session.commit()

    res = await svc.login(LoginRequest(phone="+2348011111115", password="Secret1!"))
    assert res.next_action == "pin_setup_required"
    assert res.tokens is None
    assert res.pin_setup_token is not None
    assert res.pin_set is False
