"""Settings-level validation for FCM_CREDENTIALS_JSON (Sprint 4 B25).

The FCM client lazy-loads `json.loads(self._credentials_json)` on first
push send. Before B25, a malformed env var was caught only at that
first send and silently swallowed by NotificationService._maybe_push —
every push would fail indefinitely with no startup signal. The
Pydantic validator added in B25 parses + envelope-validates at
settings-load time instead, consistent with how ELECTRICITY_DISCO_CAPS
is validated.

These tests use the Settings class directly (rather than patching
module-level `settings`) so we exercise the validator independent of
the app's existing singleton.
"""
import json

import pytest
from pydantic import ValidationError

from app.core.config import Settings


# Minimal env the Settings class requires regardless of the FCM field.
_REQUIRED_ENV = {
    "SECRET_KEY": "test-secret",
    "DATABASE_URL": "sqlite:///:memory:",
    "FIRST_SUPERUSER_EMAIL": "admin@example.com",
    "FIRST_SUPERUSER_PASSWORD": "super-secret",
}


def _make_settings(**overrides):
    """Instantiate Settings with the required fields + optional
    overrides. Pydantic's env-var precedence would otherwise leak host
    env into the test, so we pass all required fields explicitly."""
    kwargs = {**_REQUIRED_ENV, **overrides}
    return Settings(**kwargs)


# ── 1. Empty / None → untouched (fake-push fallback path) ────────────────


def test_fcm_credentials_json_none_is_accepted():
    """None is the default; tests + local dev run without FCM creds and
    fall back to FakePushClient. The validator must not reject this."""
    settings = _make_settings(FCM_CREDENTIALS_JSON=None)
    assert settings.FCM_CREDENTIALS_JSON is None


def test_fcm_credentials_json_empty_string_is_normalized_to_none():
    """Empty-string (common when an env var is set but blank) is
    equivalent to unset — normalize to None so the factory's
    _fcm_config_present() check sees the fake path."""
    settings = _make_settings(FCM_CREDENTIALS_JSON="")
    assert settings.FCM_CREDENTIALS_JSON is None


# ── 2. Well-formed service-account JSON → accepted ──────────────────────


def test_fcm_credentials_json_valid_service_account_is_accepted():
    """A well-formed service-account key (client_email + private_key
    present) passes the validator and is stored verbatim — the FCM
    client will json.loads() this string on first send."""
    sa = {
        "type": "service_account",
        "project_id": "test-project",
        "client_email": "fcm@test-project.iam.gserviceaccount.com",
        "private_key": "-----BEGIN PRIVATE KEY-----\nMIIE...fake...\n-----END PRIVATE KEY-----\n",
    }
    raw = json.dumps(sa)
    settings = _make_settings(FCM_CREDENTIALS_JSON=raw)
    assert settings.FCM_CREDENTIALS_JSON == raw


# ── 3. Malformed JSON → ValidationError at load time ────────────────────


def test_fcm_credentials_json_malformed_raises_validation_error():
    """Previously this silently passed through to the FCM client and
    blew up on first send. Now it fails at Settings() construction —
    the process won't start with bad creds."""
    with pytest.raises(ValidationError) as excinfo:
        _make_settings(FCM_CREDENTIALS_JSON="{not valid json")
    assert "must be valid JSON" in str(excinfo.value)


def test_fcm_credentials_json_non_object_raises_validation_error():
    """JSON that parses but isn't an object (array, scalar) can't be a
    service-account key — the FCM client would crash downstream trying
    to read fields off a list. Fail at load time."""
    with pytest.raises(ValidationError) as excinfo:
        _make_settings(FCM_CREDENTIALS_JSON="[1, 2, 3]")
    assert "must decode to a JSON object" in str(excinfo.value)


# ── 4. Missing required service-account fields → ValidationError ────────


def test_fcm_credentials_json_missing_client_email_raises():
    """Valid JSON but no client_email — google-auth would raise at
    refresh() time. Fail earlier so the error surfaces in ops logs
    during deploy, not mid-request."""
    sa = {
        "type": "service_account",
        "project_id": "test-project",
        "private_key": "-----BEGIN PRIVATE KEY-----\nstub\n-----END PRIVATE KEY-----\n",
    }
    with pytest.raises(ValidationError) as excinfo:
        _make_settings(FCM_CREDENTIALS_JSON=json.dumps(sa))
    assert "client_email" in str(excinfo.value)


def test_fcm_credentials_json_missing_private_key_raises():
    """Same as above for private_key — the other load-bearing field."""
    sa = {
        "type": "service_account",
        "project_id": "test-project",
        "client_email": "fcm@test-project.iam.gserviceaccount.com",
    }
    with pytest.raises(ValidationError) as excinfo:
        _make_settings(FCM_CREDENTIALS_JSON=json.dumps(sa))
    assert "private_key" in str(excinfo.value)


# ── 5. Non-string value → ValidationError ───────────────────────────────


def test_fcm_credentials_json_non_string_raises():
    """A caller passing a dict instead of a JSON string is a
    programmer error — env vars arrive as strings. Fail loudly."""
    sa = {"client_email": "x@y.z", "private_key": "k"}
    with pytest.raises(ValidationError) as excinfo:
        _make_settings(FCM_CREDENTIALS_JSON=sa)  # type: ignore[arg-type]
    # Either "must be a string" (our guard) OR pydantic's own string
    # coercion error — both are acceptable outcomes for this path.
    msg = str(excinfo.value)
    assert "string" in msg.lower() or "str" in msg.lower()
