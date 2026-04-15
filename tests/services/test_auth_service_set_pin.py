import uuid
import pytest

from app.services.auth_service import AuthService
from app.schemas.auth import RegisterRequest, VerifyEmailOtpRequest
from app.integrations.termii.fake import FakeTermiiClient
from app.integrations.email.fake import FakeEmailClient
from app.db.models.user import User


async def _register_user(svc, em, phone="+2348011111111"):
    email = f"pin_{phone[-4:]}@test.co"
    req = RegisterRequest(full_name="Pin User", phone=phone, email=email, password="Secret1!")
    res = await svc.register(req)
    code = em.sent[-1].code_or_body
    await svc.verify_email_otp(VerifyEmailOtpRequest(email=email, code=code))
    return res.user_id


@pytest.mark.asyncio
async def test_set_pin_persists(db_session):
    sms = FakeTermiiClient()
    em = FakeEmailClient()
    svc = AuthService(db=db_session, sms=sms, email=em)
    user_id_str = await _register_user(svc, em)
    user_id = uuid.UUID(user_id_str)

    await svc.set_pin(user_id, "1234")

    user = db_session.query(User).filter_by(id=user_id).one()
    assert user.pin_hash is not None


@pytest.mark.asyncio
async def test_set_pin_unknown_user(db_session):
    sms = FakeTermiiClient()
    em = FakeEmailClient()
    svc = AuthService(db=db_session, sms=sms, email=em)

    with pytest.raises(ValueError, match="USER_NOT_FOUND"):
        await svc.set_pin(uuid.uuid4(), "1234")
