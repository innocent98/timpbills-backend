import uuid
import pytest

from app.services.auth_service import AuthService
from app.schemas.auth import RegisterRequest, VerifyOtpRequest
from app.integrations.termii.fake import FakeTermiiClient
from app.db.models.user import User


async def _register_user(svc, sms, phone="+2348011111111"):
    req = RegisterRequest(full_name="Pin User", phone=phone, email=f"pin_{phone[-4:]}@test.co", password="Secret1!")
    res = await svc.register(req)
    code = sms.sent[-1].code_or_message
    await svc.verify_otp(VerifyOtpRequest(phone=phone, code=code))
    return res.user_id


@pytest.mark.asyncio
async def test_set_pin_persists(db_session):
    sms = FakeTermiiClient()
    svc = AuthService(db=db_session, sms=sms)
    user_id_str = await _register_user(svc, sms)
    user_id = uuid.UUID(user_id_str)

    await svc.set_pin(user_id, "1234")

    user = db_session.query(User).filter_by(id=user_id).one()
    assert user.pin_hash is not None


@pytest.mark.asyncio
async def test_set_pin_unknown_user(db_session):
    sms = FakeTermiiClient()
    svc = AuthService(db=db_session, sms=sms)

    with pytest.raises(ValueError, match="USER_NOT_FOUND"):
        await svc.set_pin(uuid.uuid4(), "1234")
