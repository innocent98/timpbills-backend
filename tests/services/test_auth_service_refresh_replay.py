import pytest

from app.core.security import hash_pin
from app.db.models.user import User
from app.integrations.email.fake import FakeEmailClient
from app.integrations.termii.fake import FakeTermiiClient
from app.schemas.auth import LoginRequest, RegisterRequest, VerifyEmailOtpRequest
from app.services.auth_service import AuthService


def _stamp_migration_path(db_session, *, email: str, pin: str = "8527") -> None:
    """B9: pre-stamp phone-verified + PIN so the next /verify_email_otp
    call returns full tokens (the migration branch). KYC stays tier_0."""
    user = db_session.query(User).filter(User.email == email).one()
    user.is_phone_verified = True
    user.pin_hash = hash_pin(pin)
    db_session.commit()


@pytest.mark.asyncio
async def test_refresh_rotates_jti(db_session, token_store):
    sms = FakeTermiiClient()
    em = FakeEmailClient()
    svc = AuthService(db=db_session, sms=sms, email=em, token_store=token_store)
    await svc.register(RegisterRequest(full_name="Alice", phone="+2348099999001", email="rotate@e.co", password="Secret1!"))
    _stamp_migration_path(db_session, email="rotate@e.co")
    code = em.sent[-1].code_or_body
    res1 = await svc.verify_email_otp(VerifyEmailOtpRequest(email="rotate@e.co", code=code))
    first_refresh = res1.tokens.refresh_token

    res2 = await svc.refresh(first_refresh)
    assert res2.refresh_token != first_refresh  # rotated

    # The old refresh token should no longer work (rotated out)
    with pytest.raises(Exception):
        await svc.refresh(first_refresh)


@pytest.mark.asyncio
async def test_replay_of_revoked_token_nukes_all_sessions(db_session, token_store):
    sms = FakeTermiiClient()
    em = FakeEmailClient()
    svc = AuthService(db=db_session, sms=sms, email=em, token_store=token_store)
    await svc.register(RegisterRequest(full_name="Bob", phone="+2348099999002", email="replay@e.co", password="Secret1!"))
    _stamp_migration_path(db_session, email="replay@e.co")
    code = em.sent[-1].code_or_body
    res = await svc.verify_email_otp(VerifyEmailOtpRequest(email="replay@e.co", code=code))
    # Simulate another device logging in (B12: phone-only field).
    res2 = await svc.login(LoginRequest(phone="+2348099999002", password="Secret1!"))

    # Both tokens are valid right now. Rotate the first.
    await svc.refresh(res.tokens.refresh_token)
    # Now attempt replay with original (rotated-out) refresh — should nuke all.
    with pytest.raises(Exception):
        await svc.refresh(res.tokens.refresh_token)

    # After replay detection, the second device's token should also be invalid.
    with pytest.raises(Exception):
        await svc.refresh(res2.tokens.refresh_token)


@pytest.mark.asyncio
async def test_reset_password_revokes_all_sessions(db_session, token_store):
    sms = FakeTermiiClient()
    em = FakeEmailClient()
    svc = AuthService(db=db_session, sms=sms, email=em, token_store=token_store)
    await svc.register(RegisterRequest(full_name="Carol", phone="+2348099999003", email="reset@e.co", password="Secret1!"))
    _stamp_migration_path(db_session, email="reset@e.co")
    code = em.sent[-1].code_or_body
    res = await svc.verify_email_otp(VerifyEmailOtpRequest(email="reset@e.co", code=code))

    # Trigger reset flow
    await svc.forgot_password("+2348099999003")
    reset_code = sms.sent[-1].code_or_message
    await svc.reset_password(identifier="+2348099999003", code=reset_code, new_password="NewSecret1!")

    # Old refresh token should no longer work
    with pytest.raises(Exception):
        await svc.refresh(res.tokens.refresh_token)
