"""Unit tests for DojahClient._parse_result — pure mapping logic, no network.

Sample payload shape is a best-effort reconstruction of Dojah's
verification-status response (see client.py module docstring for the
confirmed-on-integration open item). These tests lock in OUR mapping
contract regardless of exactly which Dojah field names turn out to be
correct once a live account is available.
"""
from app.core.config import settings
from app.integrations.dojah.client import DojahClient


def _client(monkeypatch) -> DojahClient:
    monkeypatch.setattr(settings, "DOJAH_API_KEY", "test-key")
    monkeypatch.setattr(settings, "DOJAH_APP_ID", "test-app-id")
    monkeypatch.setattr(settings, "DOJAH_FACE_MATCH_THRESHOLD", 70)
    return DojahClient()


def test_parse_completed_bvn_pass(monkeypatch):
    client = _client(monkeypatch)
    payload = {
        "reference_id": "KYC-BVN-1",
        "status": "Completed",
        "verification_type": "bvn",
        "id_verification": {"verified": True},
        "liveness": {"passed": True},
        "face_match": {"match": True, "confidence": 95},
        "identity": {"name": "Jane Doe", "dob": "1990-01-01"},
        "masked_id": "•••••••••17",
    }
    result = client._parse_result(payload)
    assert result.status == "success"
    assert result.verification_type == "bvn"
    assert result.id_verified is True
    assert result.liveness_passed is True
    assert result.face_match is True
    assert result.face_match_confidence == 95
    assert result.provider_reference == "KYC-BVN-1"
    assert result.identity_name == "Jane Doe"
    assert result.failure_reason is None


def test_parse_completed_low_confidence_face_match_false(monkeypatch):
    client = _client(monkeypatch)
    payload = {
        "reference_id": "KYC-NIN-2",
        "status": "Completed",
        "verification_type": "nin",
        "id_verification": {"verified": True},
        "liveness": {"passed": True},
        "face_match": {"match": True, "confidence": 40},
    }
    result = client._parse_result(payload)
    # confidence below DOJAH_FACE_MATCH_THRESHOLD (70) => face_match derived False
    assert result.face_match_confidence == 40
    assert result.face_match is False


def test_parse_pending_and_ongoing_map_to_pending(monkeypatch):
    client = _client(monkeypatch)
    for dojah_status in ("Pending", "Ongoing"):
        payload = {
            "reference_id": "KYC-BVN-3",
            "status": dojah_status,
            "verification_type": "bvn",
        }
        result = client._parse_result(payload)
        assert result.status == "pending"


def test_parse_failed_and_abandoned_map_to_failed(monkeypatch):
    client = _client(monkeypatch)
    for dojah_status in ("Failed", "Abandoned"):
        payload = {
            "reference_id": "KYC-NIN-4",
            "status": dojah_status,
            "verification_type": "nin",
            "id_verification": {"verified": False},
        }
        result = client._parse_result(payload)
        assert result.status == "failed"
        assert result.id_verified is False
