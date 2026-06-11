"""Cloudinary SDK configuration (Sprint 5c · Task 3.1).

Cloudinary's Python SDK uses module-level globals for credentials —
``cloudinary.config(...)`` sets them, and every subsequent
``cloudinary.uploader.upload(...)`` call reads from there. This helper
re-applies our credentials so the call site doesn't need to know.

Idempotent: safe to call from each ``AvatarService`` instance. When the
three required keys are unset (dev / CI without secrets) we skip the
config call entirely — any upload attempt then fails in the SDK and the
``AvatarService`` translates that to ``AvatarUploadError`` so the route
returns a clean 502.
"""
from __future__ import annotations

import cloudinary

from app.core.config import settings


def configure_cloudinary() -> None:
    """Apply the project's Cloudinary credentials to the SDK globals.

    Only runs when all three keys are present — partial credentials would
    let the SDK silently use an env-derived account, which is exactly the
    "ship-with-someone-else's-account" failure mode we want to avoid.
    """
    if not (
        settings.CLOUDINARY_CLOUD_NAME
        and settings.CLOUDINARY_API_KEY
        and settings.CLOUDINARY_API_SECRET
    ):
        return
    cloudinary.config(
        cloud_name=settings.CLOUDINARY_CLOUD_NAME,
        api_key=settings.CLOUDINARY_API_KEY,
        api_secret=settings.CLOUDINARY_API_SECRET,
        secure=True,
    )
