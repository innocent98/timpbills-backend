"""Unit tests for _check_otp_cooldown — cooldown + daily-cap gates."""
from datetime import UTC, datetime, timedelta

import pytest

from app.db.models.otp import OtpCode, OtpPurpose
from app.db.models.user import KycLevel, User
from app.services.auth_service import (
    OtpCooldownActive,
    OtpDailyCapExceeded,
    _check_otp_cooldown,
)


def _seed_user(db, *, phone="+2348011111111", email="otpcd@x.test"):
    u = User(
        phone=phone, email=email, full_name="A",
        password_hash="h", referral_code="OTPCD",
        kyc_level=KycLevel.tier_0,
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def test_cooldown_blocks_when_latest_otp_is_within_window(db_session):
    """A second send within 60s for the same (user, purpose) must raise."""
    u = _seed_user(db_session)
    db_session.add(OtpCode(
        user_id=u.id, phone=u.phone,
        code_hash="x", purpose=OtpPurpose.phone_verification,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    ))
    db_session.commit()

    with pytest.raises(OtpCooldownActive):
        _check_otp_cooldown(
            db_session,
            user_id=u.id,
            purpose=OtpPurpose.phone_verification,
        )


def test_cooldown_passes_when_latest_otp_is_outside_window(db_session):
    """An OTP older than 60s must NOT trip the cooldown."""
    u = _seed_user(db_session)
    db_session.add(OtpCode(
        user_id=u.id, phone=u.phone,
        code_hash="x", purpose=OtpPurpose.phone_verification,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        created_at=datetime.now(UTC) - timedelta(seconds=120),
    ))
    db_session.commit()

    # Does not raise
    _check_otp_cooldown(
        db_session,
        user_id=u.id,
        purpose=OtpPurpose.phone_verification,
    )


def test_cooldown_pertains_to_purpose_only(db_session):
    """An email-verify OTP within cooldown must NOT block a phone-verify send."""
    u = _seed_user(db_session)
    db_session.add(OtpCode(
        user_id=u.id, email=u.email,
        code_hash="x", purpose=OtpPurpose.email_verification,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    ))
    db_session.commit()

    # Should NOT raise — different purpose
    _check_otp_cooldown(
        db_session,
        user_id=u.id,
        purpose=OtpPurpose.phone_verification,
    )


def test_daily_cap_blocks_at_10_otps_across_any_purpose(db_session):
    """10 OTPs already today across mixed purposes must trip the cap."""
    u = _seed_user(db_session)
    today_midnight = datetime.now(UTC).replace(
        hour=0, minute=0, second=0, microsecond=0,
    )
    for i in range(10):
        purpose = (
            OtpPurpose.phone_verification if i % 2 == 0
            else OtpPurpose.email_verification
        )
        db_session.add(OtpCode(
            user_id=u.id, phone=u.phone,
            code_hash="x", purpose=purpose,
            expires_at=today_midnight + timedelta(hours=1),
            # spread across the day so the cooldown gate definitely passes
            created_at=today_midnight + timedelta(minutes=i * 30),
        ))
    db_session.commit()

    # Use password_reset (different purpose than any seeded) to ensure
    # the cooldown gate doesn't fire — only the daily cap should.
    with pytest.raises(OtpDailyCapExceeded):
        _check_otp_cooldown(
            db_session,
            user_id=u.id,
            purpose=OtpPurpose.password_reset,
        )


def test_daily_cap_ignores_otps_from_previous_days(db_session):
    """OTPs from yesterday should not count toward today's cap."""
    u = _seed_user(db_session)
    yesterday = datetime.now(UTC) - timedelta(days=1)
    for _ in range(15):
        db_session.add(OtpCode(
            user_id=u.id, phone=u.phone,
            code_hash="x", purpose=OtpPurpose.phone_verification,
            expires_at=yesterday + timedelta(hours=1),
            created_at=yesterday,
        ))
    db_session.commit()

    # No OTPs today — should NOT raise either gate
    _check_otp_cooldown(
        db_session,
        user_id=u.id,
        purpose=OtpPurpose.password_reset,
    )


def test_no_existing_otps_passes(db_session):
    """First-time send for a user (no prior OTPs) must not raise."""
    u = _seed_user(db_session)
    _check_otp_cooldown(
        db_session,
        user_id=u.id,
        purpose=OtpPurpose.phone_verification,
    )
