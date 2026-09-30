"""Startup (Settings-load) fail-fast for the KYC fake gate.

The Dojah fake approves every BVN/NIN verification. If it were selected in
staging/production (via FORCE_FAKE_PROVIDERS or a missing DOJAH_API_KEY) every
user's KYC tier would be upgraded without a real check. The factory refuses
that at call time; this validator refuses it at boot so a bad deploy never
starts serving traffic.

Settings is instantiated directly with every relevant field passed explicitly
so host / CI env vars (CI sets FORCE_FAKE_PROVIDERS=true) cannot leak in.
"""

import pytest
from pydantic import ValidationError

from app.core.config import Settings

_BASE = {
    "SECRET_KEY": "test-secret",
    "DATABASE_URL": "sqlite:///:memory:",
}


def _make(**overrides):
    return Settings(**{**_BASE, **overrides})


@pytest.mark.parametrize("env", ["production", "staging", "preview"])
def test_missing_dojah_key_outside_dev_fails_at_load(env):
    with pytest.raises(ValidationError, match="DOJAH_API_KEY"):
        _make(ENVIRONMENT=env, FORCE_FAKE_PROVIDERS=False, DOJAH_API_KEY=None)


def test_blank_dojah_key_in_production_fails_at_load():
    with pytest.raises(ValidationError, match="DOJAH_API_KEY"):
        _make(ENVIRONMENT="production", FORCE_FAKE_PROVIDERS=False, DOJAH_API_KEY="")


@pytest.mark.parametrize("env", ["production", "staging"])
def test_force_fake_outside_dev_fails_at_load(env):
    with pytest.raises(ValidationError, match="FORCE_FAKE_PROVIDERS"):
        _make(ENVIRONMENT=env, FORCE_FAKE_PROVIDERS=True, DOJAH_API_KEY="real-key")


def test_production_with_dojah_key_loads():
    s = _make(ENVIRONMENT="production", FORCE_FAKE_PROVIDERS=False, DOJAH_API_KEY="real-key")
    assert s.DOJAH_API_KEY == "real-key"


@pytest.mark.parametrize("env", ["dev", "development", "test", "testing", "local"])
def test_dev_envs_load_without_dojah_key(env):
    s = _make(ENVIRONMENT=env, FORCE_FAKE_PROVIDERS=True, DOJAH_API_KEY=None)
    assert s.DOJAH_API_KEY is None
