"""Host selection: sandbox keys must hit sandbox.dojah.io, prod api.dojah.io.

Pointing sandbox creds at the prod host returns a misleading
`401 "Your Secret Key could not be Authorized"` — regression guard for the
go-live footgun.
"""
from app.core.config import settings
from app.integrations.dojah.client import (
    _DEFAULT_PROD_HOST,
    _SANDBOX_HOST,
    _resolve_base_url,
)


def test_sandbox_env_uses_sandbox_host(monkeypatch):
    monkeypatch.setattr(settings, "DOJAH_ENVIRONMENT", "sandbox")
    monkeypatch.setattr(settings, "DOJAH_BASE_URL", _DEFAULT_PROD_HOST)  # default
    assert _resolve_base_url() == _SANDBOX_HOST


def test_production_env_uses_prod_host(monkeypatch):
    monkeypatch.setattr(settings, "DOJAH_ENVIRONMENT", "production")
    monkeypatch.setattr(settings, "DOJAH_BASE_URL", _DEFAULT_PROD_HOST)
    assert _resolve_base_url() == _DEFAULT_PROD_HOST


def test_explicit_custom_base_url_overrides(monkeypatch):
    monkeypatch.setattr(settings, "DOJAH_ENVIRONMENT", "sandbox")
    monkeypatch.setattr(settings, "DOJAH_BASE_URL", "https://proxy.internal")
    assert _resolve_base_url() == "https://proxy.internal"
