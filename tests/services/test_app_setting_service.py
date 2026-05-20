"""Tests for AppSettingService — typed getters over the AppSetting kv store.

Sprint 5b B2. Caps and reward amounts are read from app_settings on every
credit attempt; the service caches them with a short TTL so the hot path
doesn't hammer the DB. These tests pin both the type-casting contract and
the cache behaviour.
"""
from decimal import Decimal

import pytest

from app.db.models.app_setting import AppSetting
from app.services.app_setting_service import AppSettingService, AppSettingMissing


def _seed(db, key: str, value: str) -> None:
    db.add(AppSetting(key=key, value=value))
    db.commit()


def test_get_int_returns_typed_int(db_session):
    _seed(db_session, "REFERRAL_DAILY_CAP", "5")
    svc = AppSettingService(db=db_session)
    assert svc.get_int("REFERRAL_DAILY_CAP") == 5
    assert isinstance(svc.get_int("REFERRAL_DAILY_CAP"), int)


def test_get_decimal_returns_decimal(db_session):
    _seed(db_session, "REFERRAL_LIFETIME_CAP_NAIRA", "50000")
    svc = AppSettingService(db=db_session)
    out = svc.get_decimal("REFERRAL_LIFETIME_CAP_NAIRA")
    assert out == Decimal("50000")
    assert isinstance(out, Decimal)


def test_get_bool_truthy_values(db_session):
    _seed(db_session, "REFERRAL_ENABLED", "true")
    svc = AppSettingService(db=db_session)
    assert svc.get_bool("REFERRAL_ENABLED") is True


def test_get_bool_falsy_values(db_session):
    _seed(db_session, "REFERRAL_ENABLED", "false")
    svc = AppSettingService(db=db_session)
    assert svc.get_bool("REFERRAL_ENABLED") is False


def test_get_missing_raises(db_session):
    svc = AppSettingService(db=db_session)
    with pytest.raises(AppSettingMissing):
        svc.get_int("NONEXISTENT_KEY")


def test_get_with_default_returns_default_when_missing(db_session):
    svc = AppSettingService(db=db_session)
    assert svc.get_int("MISSING", default=7) == 7
    assert svc.get_bool("MISSING", default=True) is True
    assert svc.get_decimal("MISSING", default=Decimal("3.14")) == Decimal("3.14")


def test_cache_serves_stale_within_ttl(db_session):
    """After first read, mutating the row directly bypasses the cache for
    the TTL window. Confirms the cache is actually used — without it the
    second call would return the new value."""
    _seed(db_session, "REFERRAL_DAILY_CAP", "5")
    svc = AppSettingService(db=db_session, ttl_seconds=60)
    assert svc.get_int("REFERRAL_DAILY_CAP") == 5

    # Mutate without going through the service
    row = db_session.query(AppSetting).filter_by(key="REFERRAL_DAILY_CAP").one()
    row.value = "99"
    db_session.commit()

    # Still cached — old value
    assert svc.get_int("REFERRAL_DAILY_CAP") == 5


def test_cache_expires_after_ttl(db_session):
    """TTL=0 means every read goes to DB."""
    _seed(db_session, "REFERRAL_DAILY_CAP", "5")
    svc = AppSettingService(db=db_session, ttl_seconds=0)
    assert svc.get_int("REFERRAL_DAILY_CAP") == 5

    row = db_session.query(AppSetting).filter_by(key="REFERRAL_DAILY_CAP").one()
    row.value = "99"
    db_session.commit()

    # ttl=0 → fresh read
    assert svc.get_int("REFERRAL_DAILY_CAP") == 99


def test_invalidate_drops_cache(db_session):
    _seed(db_session, "REFERRAL_DAILY_CAP", "5")
    svc = AppSettingService(db=db_session, ttl_seconds=600)
    assert svc.get_int("REFERRAL_DAILY_CAP") == 5

    row = db_session.query(AppSetting).filter_by(key="REFERRAL_DAILY_CAP").one()
    row.value = "42"
    db_session.commit()

    svc.invalidate("REFERRAL_DAILY_CAP")
    assert svc.get_int("REFERRAL_DAILY_CAP") == 42
