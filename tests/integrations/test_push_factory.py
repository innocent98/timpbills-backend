"""Unit tests for the push-client factory — Sprint 4 B17.

Exercises the S3C-M1 double-gate + the new FCM config branch:
  * dev/test env + FORCE_FAKE_PROVIDERS=True  → FakePushClient
  * dev/test env + FORCE_FAKE_PROVIDERS=False + FCM_* set → FCMPushClient
  * dev/test env + no flag + no FCM config   → RuntimeError
  * non-dev env + FORCE_FAKE_PROVIDERS=True   → FakePushInEligibleEnvError
  * non-dev env + FCM_* set                   → FCMPushClient
  * non-dev env + FCM_* missing               → RuntimeError
"""
import json
import tempfile
from pathlib import Path

import pytest

from app.core.config import settings
from app.integrations.push.factory import (
    FakePushInEligibleEnvError,
    select_push_client,
)
from app.integrations.push.fake import FakePushClient


# Minimal service-account JSON. The Google auth library only validates
# `type` + `private_key` + `client_email` at construction time; .refresh()
# is what actually hits the network (which we never reach in these tests).
# RSA key below is a throwaway 1024-bit generated for tests only.
_FAKE_SA_JSON = json.dumps({
    "type": "service_account",
    "project_id": "timpbills-test",
    "private_key_id": "key-id",
    "private_key": (
        "-----BEGIN PRIVATE KEY-----\n"
        "MIICdgIBADANBgkqhkiG9w0BAQEFAASCAmAwggJcAgEAAoGBAMK0pFYlTJ9pGVq+\n"
        "-----END PRIVATE KEY-----\n"
    ),
    "client_email": "fcm-test@timpbills-test.iam.gserviceaccount.com",
    "client_id": "0",
    "token_uri": "https://oauth2.googleapis.com/token",
})


@pytest.fixture
def _clean_settings(monkeypatch):
    """Reset the push-factory-relevant settings to known state for each
    test. Also clears the module-level _fake_singleton so tests are
    order-independent."""
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")
    monkeypatch.setattr(settings, "FORCE_FAKE_PROVIDERS", False)
    monkeypatch.setattr(settings, "FCM_CREDENTIALS_PATH", "")
    monkeypatch.setattr(settings, "FCM_CREDENTIALS_JSON", "")
    monkeypatch.setattr(settings, "FCM_PROJECT_ID", "")

    from app.integrations.push import factory as f
    monkeypatch.setattr(f, "_fake_singleton", None)
    yield


def test_fake_selected_when_force_fake_and_env_test(_clean_settings, monkeypatch):
    monkeypatch.setattr(settings, "FORCE_FAKE_PROVIDERS", True)
    client = select_push_client()
    assert isinstance(client, FakePushClient)


def test_raises_when_dev_env_without_flag_and_no_fcm_config(_clean_settings):
    # Dev env, no force-fake, no FCM config — there's literally no
    # sensible client to return. Fail loudly.
    with pytest.raises(RuntimeError, match="Push client is not configured"):
        select_push_client()


def test_raises_when_force_fake_leaks_into_non_dev_env(_clean_settings, monkeypatch):
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(settings, "FORCE_FAKE_PROVIDERS", True)
    with pytest.raises(FakePushInEligibleEnvError):
        select_push_client()


def test_raises_when_non_dev_env_without_fcm_config(_clean_settings, monkeypatch):
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    with pytest.raises(RuntimeError, match="FCMPushClient is not configured"):
        select_push_client()


def test_selects_fcm_client_when_non_dev_env_and_fcm_config_set(_clean_settings, monkeypatch):
    """In a prod-like env with FCM config, the factory returns an
    FCMPushClient. We monkey-patch the FCMPushClient class with a
    lightweight stub so we don't need a real service-account key."""
    monkeypatch.setattr(settings, "ENVIRONMENT", "staging")
    monkeypatch.setattr(settings, "FCM_PROJECT_ID", "timpbills-staging")
    monkeypatch.setattr(settings, "FCM_CREDENTIALS_JSON", _FAKE_SA_JSON)

    import app.integrations.push.fcm as fcm_module

    class _StubFCM:
        def __init__(self, *a, **kw):
            self.init_kwargs = kw
    monkeypatch.setattr(fcm_module, "FCMPushClient", _StubFCM)

    client = select_push_client()
    assert isinstance(client, _StubFCM)


def test_selects_fcm_client_in_dev_env_when_fcm_config_set_and_no_flag(_clean_settings, monkeypatch):
    """Dev env runs against a real FCM sandbox if configured + no
    FORCE_FAKE_PROVIDERS flag. Lets operators smoke-test the real
    client without promoting the env string."""
    monkeypatch.setattr(settings, "FCM_PROJECT_ID", "timpbills-test")
    # Use a file path this time to exercise the other creds branch.
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "sa.json"
        p.write_text(_FAKE_SA_JSON)
        monkeypatch.setattr(settings, "FCM_CREDENTIALS_PATH", str(p))

        import app.integrations.push.fcm as fcm_module

        class _StubFCM:
            def __init__(self, *a, **kw):
                self.init_kwargs = kw
        monkeypatch.setattr(fcm_module, "FCMPushClient", _StubFCM)

        client = select_push_client()
        assert isinstance(client, _StubFCM)
