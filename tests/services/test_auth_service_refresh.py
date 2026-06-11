import pytest

from app.core.security import hash_pin
from app.db.models.user import User
from app.integrations.email.fake import FakeEmailClient
from app.integrations.termii.fake import FakeTermiiClient
from app.schemas.auth import RegisterRequest, VerifyEmailOtpRequest
from app.services.auth_service import AuthService
from app.services.token_store import NullTokenStore


async def _get_tokens(svc, em, db_session):
    """B9: /verify_email_otp only issues tokens on the migration branch
    (phone-verified + PIN-set). Pre-stamp the row so this helper still
    yields an AuthTokens object the refresh tests can use."""
    req = RegisterRequest(
        full_name="Refresh User", phone="+2348011111111",
        email="refresh@test.co", password="Secret1!",
    )
    await svc.register(req)

    user = db_session.query(User).filter(User.email == "refresh@test.co").one()
    user.is_phone_verified = True
    user.pin_hash = hash_pin("8527")
    db_session.commit()

    code = em.sent[-1].code_or_body
    verify_res = await svc.verify_email_otp(
        VerifyEmailOtpRequest(email="refresh@test.co", code=code)
    )
    assert verify_res.tokens is not None, verify_res
    return verify_res.tokens


@pytest.mark.asyncio
async def test_refresh_issues_new_token_pair(db_session):
    sms = FakeTermiiClient()
    em = FakeEmailClient()
    svc = AuthService(db=db_session, sms=sms, email=em, token_store=NullTokenStore())
    tokens = await _get_tokens(svc, em, db_session)

    new_tokens = await svc.refresh(tokens.refresh_token)
    assert new_tokens.access_token
    assert new_tokens.refresh_token
    # New refresh token should have a different jti (token rotation)
    assert new_tokens.refresh_token != tokens.refresh_token


@pytest.mark.asyncio
async def test_refresh_rejects_access_token(db_session):
    sms = FakeTermiiClient()
    em = FakeEmailClient()
    svc = AuthService(db=db_session, sms=sms, email=em, token_store=NullTokenStore())
    tokens = await _get_tokens(svc, em, db_session)

    with pytest.raises(ValueError, match="INVALID_TOKEN"):
        await svc.refresh(tokens.access_token)


@pytest.mark.asyncio
async def test_refresh_rejects_garbage_token(db_session):
    sms = FakeTermiiClient()
    em = FakeEmailClient()
    svc = AuthService(db=db_session, sms=sms, email=em, token_store=NullTokenStore())

    with pytest.raises(ValueError, match="INVALID_TOKEN"):
        await svc.refresh("this.is.garbage")
