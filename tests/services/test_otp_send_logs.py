"""Task 13: every OTP SMS send writes a notification_logs row.

All OTP-SMS sites in AuthService funnel through ``_send_otp_sms``, which
records a pending row, fires the Termii send, then marks the row sent. We
assert the audit row exists with event="otp", provider="termii", and
status=sent for two representative paths: the phone OTP that fires on
email-verify (the phone gate becoming active) and forgot_password.
"""
import pytest

from app.db.models.notification_log import (
    NotificationChannel,
    NotificationLog,
    NotificationLogStatus,
)
from app.db.models.otp import OtpPurpose  # noqa: F401  (parallels existing tests)
from app.integrations.base import SmsSendError
from app.integrations.email.fake import FakeEmailClient
from app.integrations.termii.fake import FakeTermiiClient
from app.schemas.auth import RegisterRequest, VerifyEmailOtpRequest
from app.services.auth_service import AuthService
from app.services.token_store import NullTokenStore


async def _register(svc, em, phone="+2348011111111"):
    email = f"otplog_{phone[-4:]}@test.co"
    req = RegisterRequest(
        full_name="OTP Log User", phone=phone, email=email, password="Secret1!",
    )
    await svc.register(req)
    code = em.sent[-1].code_or_body
    await svc.verify_email_otp(VerifyEmailOtpRequest(email=email, code=code))
    return phone


def _sms_rows(db):
    return (
        db.query(NotificationLog)
        .filter(NotificationLog.channel == NotificationChannel.sms)
        .all()
    )


@pytest.mark.asyncio
async def test_email_verify_writes_otp_sms_log(db_session):
    """The phone OTP that fires on email-verify (phone gate becoming
    active) funnels through ``_send_otp_sms`` and writes the audit row.
    Register itself sends no SMS."""
    sms = FakeTermiiClient()
    em = FakeEmailClient()
    svc = AuthService(db=db_session, sms=sms, email=em, token_store=NullTokenStore())

    await svc.register(
        RegisterRequest(
            full_name="Reg User", phone="+2348011112222",
            email="reglog@test.co", password="Secret1!",
        )
    )
    # Baseline: register sent no SMS and wrote no SMS audit row.
    assert len(sms.sent) == 0
    assert _sms_rows(db_session) == []

    # Verifying email activates the phone gate → one phone OTP SMS.
    code = em.sent[-1].code_or_body
    await svc.verify_email_otp(VerifyEmailOtpRequest(email="reglog@test.co", code=code))

    assert len(sms.sent) == 1
    rows = _sms_rows(db_session)
    assert len(rows) == 1
    row = rows[0]
    assert row.event == "otp"
    assert row.provider == "termii"
    assert row.status is NotificationLogStatus.sent
    assert row.sent_at is not None


@pytest.mark.asyncio
async def test_forgot_password_writes_otp_sms_log(db_session):
    sms = FakeTermiiClient()
    em = FakeEmailClient()
    svc = AuthService(db=db_session, sms=sms, email=em, token_store=NullTokenStore())

    phone = await _register(svc, em)
    # Drop the register-phase SMS rows so we isolate the forgot-password send.
    for r in _sms_rows(db_session):
        db_session.delete(r)
    db_session.commit()
    sms.sent.clear()

    await svc.forgot_password(phone)

    assert len(sms.sent) == 1
    rows = _sms_rows(db_session)
    assert len(rows) == 1
    row = rows[0]
    assert row.event == "otp"
    assert row.provider == "termii"
    assert row.status is NotificationLogStatus.sent
    assert row.sent_at is not None


class _FailingSms(FakeTermiiClient):
    """SMS provider that raises like the real Termii client does on an
    in-band failure (insufficient balance / unapproved sender ID)."""

    async def send_otp(self, *, phone: str, code: str) -> None:
        raise SmsSendError("termii send failed: Insufficient balance")


@pytest.mark.asyncio
async def test_send_failure_marks_log_failed_and_propagates(db_session):
    """When the provider raises (in-band Termii error), the funnel must
    mark the notification_logs row failed and re-raise — callers depend
    on the raise to surface delivery failure. The phone OTP now fires on
    email-verify, so that's where the failure surfaces.

    The ``except (OtpCooldownActive, OtpDailyCapExceeded)`` guard in
    verify_email_otp must NOT swallow a provider SmsSendError — it
    propagates so mobile sees the delivery failure."""
    sms = _FailingSms()
    em = FakeEmailClient()
    svc = AuthService(db=db_session, sms=sms, email=em, token_store=NullTokenStore())

    await svc.register(
        RegisterRequest(
            full_name="Fail User", phone="+2348011113333",
            email="faillog@test.co", password="Secret1!",
        )
    )
    code = em.sent[-1].code_or_body

    with pytest.raises(SmsSendError):
        await svc.verify_email_otp(
            VerifyEmailOtpRequest(email="faillog@test.co", code=code)
        )

    rows = _sms_rows(db_session)
    assert len(rows) == 1
    row = rows[0]
    assert row.status is NotificationLogStatus.failed
    assert "Insufficient balance" in (row.error or "")
