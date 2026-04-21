"""Webhook shared-secret helper — timing-safe, refuses when misconfigured."""
import pytest

from app.core.config import settings
from app.integrations.vtpass.signature import (
    WebhookSecretNotConfigured,
    verify_vtpass_secret,
)


def test_accepts_matching_secret(monkeypatch):
    monkeypatch.setattr(settings, "VTPASS_WEBHOOK_SECRET", "s3cret-xyz")
    assert verify_vtpass_secret(header_value="s3cret-xyz") is True


def test_rejects_wrong_secret(monkeypatch):
    monkeypatch.setattr(settings, "VTPASS_WEBHOOK_SECRET", "s3cret-xyz")
    assert verify_vtpass_secret(header_value="wrong") is False


def test_rejects_missing_header(monkeypatch):
    monkeypatch.setattr(settings, "VTPASS_WEBHOOK_SECRET", "s3cret-xyz")
    assert verify_vtpass_secret(header_value=None) is False
    assert verify_vtpass_secret(header_value="") is False


def test_raises_when_server_secret_unset(monkeypatch):
    """Critical: if VTPASS_WEBHOOK_SECRET is empty we refuse loudly
    rather than silently accepting any header value."""
    monkeypatch.setattr(settings, "VTPASS_WEBHOOK_SECRET", None)
    with pytest.raises(WebhookSecretNotConfigured):
        verify_vtpass_secret(header_value="anything")


def test_raises_when_server_secret_empty_string(monkeypatch):
    monkeypatch.setattr(settings, "VTPASS_WEBHOOK_SECRET", "")
    with pytest.raises(WebhookSecretNotConfigured):
        verify_vtpass_secret(header_value="anything")
