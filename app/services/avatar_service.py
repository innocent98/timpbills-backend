"""AvatarService — uploads user avatars to Cloudinary (Sprint 5c · Task 3.1).

Layered above the Cloudinary SDK to keep the route handler thin and
testable. The SDK call site is patched directly in unit tests; the
service itself owns the size + MIME guards so they're enforced
regardless of which transport (HTTP, future Celery re-upload, etc.)
calls it.

Cloudinary's public_id is set to the user UUID — uploading a new
avatar for the same user overwrites the previous asset, which we
want: only one avatar per user lives in the cloud at a time. The
``overwrite=True`` flag enables that. The returned ``secure_url`` is
what we persist on ``users.avatar_url``.
"""
from __future__ import annotations

import cloudinary
import cloudinary.uploader

from app.integrations.cloudinary_client import configure_cloudinary

MAX_BYTES = 5 * 1024 * 1024
ALLOWED_MIME = {"image/jpeg", "image/png"}


class AvatarUploadError(Exception):
    """Raised on any client-visible upload failure.

    Carries a human message; the route handler inspects the message to
    decide between 413 (oversize), 415 (wrong MIME), and 502 (upstream).
    """


class AvatarService:
    """Owns the avatar upload contract.

    Constructor takes credentials explicitly so tests can instantiate
    with dummy strings without touching real settings. When all three
    creds are present we apply them to the SDK at construction; when
    any are missing the SDK calls below will fail and the
    ``AvatarUploadError`` translation kicks in at the route.
    """

    def __init__(
        self,
        cloud_name: str | None,
        api_key: str | None,
        api_secret: str | None,
    ) -> None:
        self.cloud_name = cloud_name
        self.api_key = api_key
        self.api_secret = api_secret
        if cloud_name and api_key and api_secret:
            configure_cloudinary()

    def upload_avatar(
        self,
        *,
        user_id: str,
        file_bytes: bytes,
        content_type: str,
    ) -> str:
        """Validate + upload the avatar; return the Cloudinary secure URL.

        Raises ``AvatarUploadError`` with a message the route handler
        pattern-matches to pick the right HTTP status:
          * "exceeds 5MB"   → 413
          * "JPEG or PNG"   → 415
          * "upstream …"    → 502
        """
        if len(file_bytes) > MAX_BYTES:
            raise AvatarUploadError("Image exceeds 5MB limit")
        if content_type not in ALLOWED_MIME:
            raise AvatarUploadError("Image must be JPEG or PNG")
        try:
            resp = cloudinary.uploader.upload(
                file_bytes,
                folder="timpbills/avatars",
                public_id=user_id,
                overwrite=True,
                resource_type="image",
            )
        except Exception as e:  # noqa: BLE001 — translate every SDK error
            raise AvatarUploadError(f"upstream Cloudinary failure: {e}") from e
        return resp["secure_url"]
