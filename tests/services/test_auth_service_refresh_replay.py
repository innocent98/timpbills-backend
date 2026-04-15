import pytest
from app.services.auth_service import AuthService
from app.schemas.auth import RegisterRequest, LoginRequest, VerifyOtpRequest
from app.integrations.termii.fake import FakeTermiiClient


@pytest.mark.asyncio
async def test_refresh_rotates_jti(db_session, token_store):
    sms = FakeTermiiClient()
    svc = AuthService(db=db_session, sms=sms, token_store=token_store)
    await svc.register(RegisterRequest(full_name="Alice", phone="+2348099999001", email="rotate@e.co", password="Secret1!"))
    code = sms.sent[-1].code_or_message
    res1 = await svc.verify_otp(VerifyOtpRequest(phone="+2348099999001", code=code))
    first_refresh = res1.tokens.refresh_token

    res2 = await svc.refresh(first_refresh)
    assert res2.refresh_token != first_refresh  # rotated

    # The old refresh token should no longer work (rotated out)
    with pytest.raises(Exception):
        await svc.refresh(first_refresh)


@pytest.mark.asyncio
async def test_replay_of_revoked_token_nukes_all_sessions(db_session, token_store):
    sms = FakeTermiiClient()
    svc = AuthService(db=db_session, sms=sms, token_store=token_store)
    await svc.register(RegisterRequest(full_name="Bob", phone="+2348099999002", email="replay@e.co", password="Secret1!"))
    code = sms.sent[-1].code_or_message
    res = await svc.verify_otp(VerifyOtpRequest(phone="+2348099999002", code=code))
    # Simulate another device logging in
    res2 = await svc.login(LoginRequest(identifier="+2348099999002", password="Secret1!"))

    # Both tokens are valid right now. Rotate the first.
    res3 = await svc.refresh(res.tokens.refresh_token)
    # Now attempt replay with original (rotated-out) refresh — should nuke all.
    with pytest.raises(Exception):
        await svc.refresh(res.tokens.refresh_token)

    # After replay detection, the second device's token should also be invalid.
    with pytest.raises(Exception):
        await svc.refresh(res2.tokens.refresh_token)


@pytest.mark.asyncio
async def test_reset_password_revokes_all_sessions(db_session, token_store):
    sms = FakeTermiiClient()
    svc = AuthService(db=db_session, sms=sms, token_store=token_store)
    await svc.register(RegisterRequest(full_name="Carol", phone="+2348099999003", email="reset@e.co", password="Secret1!"))
    code = sms.sent[-1].code_or_message
    res = await svc.verify_otp(VerifyOtpRequest(phone="+2348099999003", code=code))

    # Trigger reset flow
    await svc.forgot_password("+2348099999003")
    reset_code = sms.sent[-1].code_or_message
    await svc.reset_password(identifier="+2348099999003", code=reset_code, new_password="NewSecret1!")

    # Old refresh token should no longer work
    with pytest.raises(Exception):
        await svc.refresh(res.tokens.refresh_token)
