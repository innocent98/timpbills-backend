import pytest

from app.services.auth_service import AuthService
from app.schemas.auth import RegisterRequest, LoginRequest, VerifyOtpRequest
from app.integrations.termii.fake import FakeTermiiClient


async def _get_tokens(svc, sms):
    req = RegisterRequest(full_name="Refresh User", phone="+2348011111111", email="refresh@test.co", password="Secret1!")
    await svc.register(req)
    code = sms.sent[-1].code_or_message
    verify_res = await svc.verify_otp(VerifyOtpRequest(phone="+2348011111111", code=code))
    return verify_res.tokens


@pytest.mark.asyncio
async def test_refresh_issues_new_token_pair(db_session):
    sms = FakeTermiiClient()
    svc = AuthService(db=db_session, sms=sms)
    tokens = await _get_tokens(svc, sms)

    new_tokens = await svc.refresh(tokens.refresh_token)
    assert new_tokens.access_token
    assert new_tokens.refresh_token
    # New refresh token should have a different jti (token rotation)
    assert new_tokens.refresh_token != tokens.refresh_token


@pytest.mark.asyncio
async def test_refresh_rejects_access_token(db_session):
    sms = FakeTermiiClient()
    svc = AuthService(db=db_session, sms=sms)
    tokens = await _get_tokens(svc, sms)

    with pytest.raises(ValueError, match="INVALID_TOKEN"):
        await svc.refresh(tokens.access_token)


@pytest.mark.asyncio
async def test_refresh_rejects_garbage_token(db_session):
    sms = FakeTermiiClient()
    svc = AuthService(db=db_session, sms=sms)

    with pytest.raises(ValueError, match="INVALID_TOKEN"):
        await svc.refresh("this.is.garbage")
