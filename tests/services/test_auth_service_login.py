import pytest

from app.services.auth_service import AuthService
from app.schemas.auth import RegisterRequest, LoginRequest, VerifyEmailOtpRequest
from app.integrations.termii.fake import FakeTermiiClient
from app.integrations.email.fake import FakeEmailClient
from app.services.token_store import NullTokenStore
from app.db.models.user import User


async def _register_and_verify(svc, email_client, phone="+2348011111111", email="user@test.co"):
    req = RegisterRequest(full_name="Login User", phone=phone, email=email, password="Secret1!")
    await svc.register(req)
    code = email_client.sent[-1].code_or_body
    await svc.verify_email_otp(VerifyEmailOtpRequest(email=email, code=code))


@pytest.mark.asyncio
async def test_login_with_email(db_session):
    sms = FakeTermiiClient()
    em = FakeEmailClient()
    svc = AuthService(db=db_session, sms=sms, email=em, token_store=NullTokenStore())
    await _register_and_verify(svc, em)

    res = await svc.login(LoginRequest(identifier="user@test.co", password="Secret1!"))
    assert res.tokens.access_token
    assert res.tokens.refresh_token
    assert res.pin_set is False


@pytest.mark.asyncio
async def test_login_with_phone(db_session):
    sms = FakeTermiiClient()
    em = FakeEmailClient()
    svc = AuthService(db=db_session, sms=sms, email=em, token_store=NullTokenStore())
    await _register_and_verify(svc, em)

    res = await svc.login(LoginRequest(identifier="+2348011111111", password="Secret1!"))
    assert res.tokens.access_token


@pytest.mark.asyncio
async def test_login_wrong_password(db_session):
    sms = FakeTermiiClient()
    em = FakeEmailClient()
    svc = AuthService(db=db_session, sms=sms, email=em, token_store=NullTokenStore())
    await _register_and_verify(svc, em)

    with pytest.raises(ValueError, match="INVALID_CREDENTIALS"):
        await svc.login(LoginRequest(identifier="user@test.co", password="WrongPass1!"))


@pytest.mark.asyncio
async def test_login_unknown_user(db_session):
    sms = FakeTermiiClient()
    em = FakeEmailClient()
    svc = AuthService(db=db_session, sms=sms, email=em, token_store=NullTokenStore())

    with pytest.raises(ValueError, match="INVALID_CREDENTIALS"):
        await svc.login(LoginRequest(identifier="ghost@test.co", password="Secret1!"))


@pytest.mark.asyncio
async def test_login_rejects_unverified_email(db_session):
    sms = FakeTermiiClient()
    em = FakeEmailClient()
    svc = AuthService(db=db_session, sms=sms, email=em, token_store=NullTokenStore())

    # Register but do NOT verify email
    req = RegisterRequest(full_name="Unverified User", phone="+2348011111112", email="unverified@test.co", password="Secret1!")
    await svc.register(req)

    with pytest.raises(ValueError, match="EMAIL_NOT_VERIFIED"):
        await svc.login(LoginRequest(identifier="unverified@test.co", password="Secret1!"))


@pytest.mark.asyncio
async def test_login_rejects_inactive_user(db_session):
    sms = FakeTermiiClient()
    em = FakeEmailClient()
    svc = AuthService(db=db_session, sms=sms, email=em, token_store=NullTokenStore())

    # Register and fully verify
    await _register_and_verify(svc, em, phone="+2348011111113", email="inactive@test.co")

    # Disable the user directly in DB
    user = db_session.query(User).filter_by(email="inactive@test.co").one()
    user.is_active = False
    db_session.commit()

    with pytest.raises(ValueError, match="ACCOUNT_DISABLED"):
        await svc.login(LoginRequest(identifier="inactive@test.co", password="Secret1!"))
