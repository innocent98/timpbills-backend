"""Unit tests for AvatarService (Sprint 5c · Task 3.1).

The Cloudinary SDK call is patched at the module-attribute level
(``app.services.avatar_service.cloudinary.uploader.upload``) so no real
network traffic happens. The MIME and size guards run before the
upload call so they never reach the patched function.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from app.services.avatar_service import AvatarService, AvatarUploadError


def _make_service() -> AvatarService:
    return AvatarService(cloud_name="x", api_key="x", api_secret="x")


def test_upload_rejects_oversize_file():
    svc = _make_service()
    huge = b"\0" * (6 * 1024 * 1024)
    with pytest.raises(AvatarUploadError, match="exceeds 5MB"):
        svc.upload_avatar(user_id="u1", file_bytes=huge, content_type="image/jpeg")


def test_upload_rejects_wrong_mime():
    svc = _make_service()
    with pytest.raises(AvatarUploadError, match="JPEG or PNG"):
        svc.upload_avatar(user_id="u1", file_bytes=b"\0", content_type="image/gif")


def test_upload_accepts_png_mime():
    svc = _make_service()
    mock_resp = {"secure_url": "https://res.cloudinary.com/x/avatar/u1.png"}
    with patch(
        "app.services.avatar_service.cloudinary.uploader.upload",
        return_value=mock_resp,
    ):
        url = svc.upload_avatar(
            user_id="u1", file_bytes=b"pngbytes", content_type="image/png"
        )
        assert url == mock_resp["secure_url"]


def test_upload_calls_cloudinary_with_correct_params():
    svc = _make_service()
    mock_resp = {"secure_url": "https://res.cloudinary.com/x/avatar/u1.jpg"}
    with patch(
        "app.services.avatar_service.cloudinary.uploader.upload",
        return_value=mock_resp,
    ) as mock_upload:
        url = svc.upload_avatar(
            user_id="u1", file_bytes=b"jpegbytes", content_type="image/jpeg"
        )
        assert url == mock_resp["secure_url"]
        kwargs = mock_upload.call_args.kwargs
        assert kwargs["folder"] == "timpbills/avatars"
        assert kwargs["public_id"] == "u1"
        assert kwargs["overwrite"] is True
        assert kwargs["resource_type"] == "image"


def test_upload_raises_on_cloudinary_failure():
    svc = _make_service()
    with patch(
        "app.services.avatar_service.cloudinary.uploader.upload",
        side_effect=Exception("oops"),
    ):
        with pytest.raises(AvatarUploadError, match="upstream"):
            svc.upload_avatar(
                user_id="u1", file_bytes=b"jpeg", content_type="image/jpeg"
            )


def test_service_constructs_without_credentials():
    """When all creds are None the constructor must not raise — it just
    skips SDK configuration. Any subsequent upload will fail on the
    Cloudinary side and be translated to AvatarUploadError, which the
    route surfaces as 502."""
    AvatarService(cloud_name=None, api_key=None, api_secret=None)
