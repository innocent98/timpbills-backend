import pytest

from app.services.auth_service import AuthService
from app.schemas.auth import RegisterRequest, LoginRequest, VerifyEmailOtpRequest
from app.integrations.termii.fake import FakeTermiiClient
from app.integrations.email.fake import FakeEmailClient
from app.services.token_store import NullTokenStore


async def _get_tokens(svc, em):
    req = RegisterRequest(full_name="Refresh User", phone="+2348011111111", email="refresh@test.co", password="Secret1!")
    await svc.register(req)
    code = em.sent[-1].code_or_body
    verify_res = await svc.verify_email_otp(VerifyEmailOtpRequest(email="refresh@test.co", code=code))
    return verify_res.tokens


@pytest.mark.asyncio
async def test_refresh_issues_new_token_pair(db_session):
    sms = FakeTermiiClient()
    em = FakeEmailClient()
    svc = AuthService(db=db_session, sms=sms, email=em, token_store=NullTokenStore())
    tokens = await _get_tokens(svc, em)

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
    tokens = await _get_tokens(svc, em)

    with pytest.raises(ValueError, match="INVALID_TOKEN"):
        await svc.refresh(tokens.access_token)


@pytest.mark.asyncio
async def test_refresh_rejects_garbage_token(db_session):
    sms = FakeTermiiClient()
    em = FakeEmailClient()
    svc = AuthService(db=db_session, sms=sms, email=em, token_store=NullTokenStore())

    with pytest.raises(ValueError, match="INVALID_TOKEN"):
        await svc.refresh("this.is.garbage")
