"""Dojah KYC provider selection — env-allowlist gate.

The fake approves every verification, so selecting it outside dev/test is a
KYC/AML bypass (tier upgrades lift wallet caps). It must be refused loudly in
any non-eligible environment, mirroring the Termii / Paystack factories.
"""

import pytest

from app.core.config import settings
from app.integrations.dojah.client import DojahClient
from app.integrations.dojah.factory import (
    FakeKycInEligibleEnvError,
    get_kyc_provider,
)
from app.integrations.dojah.fake import FakeKycProvider


def _configure(monkeypatch, *, env, force_fake, api_key):
    monkeypatch.setattr(settings, "ENVIRONMENT", env)
    monkeypatch.setattr(settings, "FORCE_FAKE_PROVIDERS", force_fake)
    monkeypatch.setattr(settings, "DOJAH_API_KEY", api_key)
    monkeypatch.setattr(settings, "DOJAH_APP_ID", "some-app-id")


# ── Eligible (dev/test) environments ─────────────────────────────────────


def test_factory_uses_fake_when_forced_in_test_env(monkeypatch):
    _configure(monkeypatch, env="test", force_fake=True, api_key="some-key")
    assert isinstance(get_kyc_provider(), FakeKycProvider)


def test_factory_uses_fake_when_key_unset_in_test_env(monkeypatch):
    _configure(monkeypatch, env="test", force_fake=False, api_key=None)
    assert isinstance(get_kyc_provider(), FakeKycProvider)


@pytest.mark.parametrize("env", ["dev", "development", "testing", "local", "TEST"])
def test_factory_allows_fake_in_every_eligible_env(monkeypatch, env):
    _configure(monkeypatch, env=env, force_fake=True, api_key=None)
    assert isinstance(get_kyc_provider(), FakeKycProvider)


def test_factory_uses_real_client_when_configured(monkeypatch):
    _configure(monkeypatch, env="test", force_fake=False, api_key="some-key")
    assert isinstance(get_kyc_provider(), DojahClient)


# ── Non-eligible environments refuse the fake ────────────────────────────


@pytest.mark.parametrize("env", ["production", "staging", "preview", "prod"])
def test_factory_raises_when_key_missing_outside_dev(monkeypatch, env):
    _configure(monkeypatch, env=env, force_fake=False, api_key=None)
    with pytest.raises(FakeKycInEligibleEnvError, match="DOJAH_API_KEY"):
        get_kyc_provider()


def test_factory_raises_when_key_blank_in_production(monkeypatch):
    # An unrendered secret commonly arrives as an empty string, not None.
    _configure(monkeypatch, env="production", force_fake=False, api_key="")
    with pytest.raises(FakeKycInEligibleEnvError, match="DOJAH_API_KEY"):
        get_kyc_provider()


@pytest.mark.parametrize("env", ["production", "staging"])
def test_factory_raises_when_forced_fake_outside_dev(monkeypatch, env):
    _configure(monkeypatch, env=env, force_fake=True, api_key="some-key")
    with pytest.raises(FakeKycInEligibleEnvError, match="FORCE_FAKE_PROVIDERS"):
        get_kyc_provider()


def test_factory_uses_real_client_in_production_when_configured(monkeypatch):
    _configure(monkeypatch, env="production", force_fake=False, api_key="some-key")
    assert isinstance(get_kyc_provider(), DojahClient)
