"""AppSettingService — typed getters over the dumb AppSetting kv store.

Sprint 5b B2. The AppSetting table stores raw strings; this service is
the single place that knows how to coerce them into ints / bools /
Decimals + a tiny TTL cache so the credit pipeline doesn't query the DB
on every cap check.

The cache is per-instance, not module-global, so each request scope owns
its own cache. Lifetime ~= request duration in API context; for the
nightly sweeper the worker holds one instance per run.
"""
from __future__ import annotations

import time
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy.orm import Session

from app.db.models.app_setting import AppSetting


class AppSettingMissing(KeyError):
    """Raised when a key is missing AND no default was supplied."""


# Sentinel — distinguishes "default not supplied" from "default is None".
_UNSET: Any = object()


# String values that count as True for get_bool. Anything else is False.
# Keep tight rather than permissive — accidental "yes" / "1" / etc. is
# safer to reject than silently honour, since these toggles can disable
# referral-system features in production.
_TRUTHY = {"true", "True", "TRUE"}
_FALSY = {"false", "False", "FALSE"}


class AppSettingService:
    """Typed accessors over the ``app_settings`` table with TTL caching.

    Cache is keyed by (key) and stores (value_string, expires_at). On
    hit-within-TTL we skip the DB; on miss or expiry we re-read.
    """

    def __init__(self, *, db: Session, ttl_seconds: int = 60) -> None:
        self._db = db
        self._ttl = ttl_seconds
        # key → (raw_value, expires_at_monotonic)
        self._cache: dict[str, tuple[str, float]] = {}

    # ── Public API ───────────────────────────────────────────────────

    def get_int(self, key: str, *, default: Any = _UNSET) -> int:
        raw = self._get_raw(key, default=default)
        if raw is _UNSET:
            # No row and no default — _get_raw would have raised; this
            # branch only fires when the caller passed default=_UNSET
            # explicitly, which they shouldn't.
            raise AppSettingMissing(key)
        if raw is default and default is not _UNSET:
            return default
        return int(raw)

    def get_bool(self, key: str, *, default: Any = _UNSET) -> bool:
        raw = self._get_raw(key, default=default)
        if raw is default and default is not _UNSET:
            return default
        if raw in _TRUTHY:
            return True
        if raw in _FALSY:
            return False
        # Anything else: treat as False but don't raise — ops may have
        # typed "no" or "0" and we'd rather degrade closed than crash
        # the credit pipeline.
        return False

    def get_decimal(self, key: str, *, default: Any = _UNSET) -> Decimal:
        raw = self._get_raw(key, default=default)
        if raw is default and default is not _UNSET:
            return default
        try:
            return Decimal(raw)
        except (InvalidOperation, TypeError) as exc:
            raise AppSettingMissing(
                f"app_setting {key!r} value {raw!r} is not a valid Decimal: {exc}"
            ) from exc

    def invalidate(self, key: str) -> None:
        """Drop a key's cached value. Next read goes back to the DB."""
        self._cache.pop(key, None)

    def invalidate_all(self) -> None:
        self._cache.clear()

    # ── Internals ────────────────────────────────────────────────────

    def _get_raw(self, key: str, *, default: Any = _UNSET) -> Any:
        """Return the raw string value (cached if fresh), or the default
        if missing. Raises AppSettingMissing only when there's no row and
        no default was supplied."""
        now = time.monotonic()
        cached = self._cache.get(key)
        if cached is not None:
            value, expires_at = cached
            if now < expires_at:
                return value
            # Stale — fall through to re-read

        row = self._db.query(AppSetting).filter(AppSetting.key == key).first()
        if row is None:
            if default is _UNSET:
                raise AppSettingMissing(key)
            return default

        self._cache[key] = (row.value, now + self._ttl)
        return row.value
