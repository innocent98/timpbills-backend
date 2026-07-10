"""Unit tests for DojahClient._parse_result — pure mapping logic, no network.

Payload shape matches Dojah's confirmed get-verification-details response
(https://docs.dojah.io/docs/technical-reference/get-verification-details):
top-level `verification_status`, plus `data.government_data.{status,data}`
and `data.selfie.status`.
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
        "verification_status": "Completed",
        "data": {
            "government_data": {
                "status": True,
                "data": {
                    "bvn": {
                        "bvn": "12345678917",
                        "first_name": "Jane",
                        "last_name": "Doe",
                        "date_of_birth": "1990-01-01",
                    }
                },
            },
            "selfie": {"status": True},
        },
    }
    result = client._parse_result(payload)
    assert result.status == "success"
    assert result.verification_type == "bvn"
    assert result.id_verified is True
    assert result.liveness_passed is True
    assert result.face_match is True
    assert result.face_match_confidence == 100
    assert result.masked_id == "•" * 9 + "17"
    assert result.provider_reference == "KYC-BVN-1"
    assert result.identity_name == "Jane Doe"
    assert result.identity_dob is not None
    assert result.failure_reason is None


def test_parse_selfie_failed_marks_face_match_and_status_false(monkeypatch):
    client = _client(monkeypatch)
    payload = {
        "reference_id": "KYC-NIN-2",
        "verification_status": "Failed",
        "data": {
            "government_data": {
                "status": True,
                "data": {
                    "nin": {
                        "nin": "98765432109",
                        "first_name": "John",
                        "last_name": "Smith",
                    }
                },
            },
            "selfie": {"status": False},
        },
    }
    result = client._parse_result(payload)
    assert result.status == "failed"
    assert result.verification_type == "nin"
    assert result.id_verified is True
    assert result.liveness_passed is False
    assert result.face_match is False
    assert result.face_match_confidence == 0
    assert result.masked_id == "•" * 9 + "09"


def test_parse_pending_status(monkeypatch):
    client = _client(monkeypatch)
    payload = {
        "reference_id": "KYC-BVN-3",
        "verification_status": "Pending",
        "data": {
            "government_data": {"status": False, "data": {}},
            "selfie": {"status": False},
        },
    }
    result = client._parse_result(payload)
    assert result.status == "pending"
    assert result.id_verified is False
    assert result.face_match is False
    assert result.masked_id == "••"
    assert result.identity_name is None
    assert result.identity_dob is None


def test_parse_ongoing_and_abandoned_map_correctly(monkeypatch):
    client = _client(monkeypatch)
    for dojah_status, expected in (("Ongoing", "pending"), ("Abandoned", "failed")):
        payload = {
            "reference_id": "KYC-BVN-4",
            "verification_status": dojah_status,
            "data": {
                "government_data": {"status": False, "data": {}},
                "selfie": {"status": False},
            },
        }
        result = client._parse_result(payload)
        assert result.status == expected


def test_parse_missing_data_branch_defaults_safely(monkeypatch):
    client = _client(monkeypatch)
    payload = {"reference_id": "KYC-BVN-5", "verification_status": "Failed"}
    result = client._parse_result(payload)
    assert result.status == "failed"
    assert result.verification_type == "bvn"
    assert result.id_verified is False
    assert result.liveness_passed is False
    assert result.face_match is False
    assert result.masked_id == "••"
