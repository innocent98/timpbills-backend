import json
from decimal import Decimal
from typing import Any

from pydantic import EmailStr, field_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Project Info
    PROJECT_NAME: str = "timpbills-backend"
    VERSION: str = "0.1.0"
    API_V1_STR: str = "/api/v1"
    ENVIRONMENT: str = "development"

    # Server
    SERVER_HOST: str = "http://localhost"
    SERVER_PORT: int = 8000

    # Debug
    DEBUG: bool = False

    # Security
    SECRET_KEY: str
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24 * 7  # 7 days
    ALGORITHM: str = "HS256"

    # CORS
    BACKEND_CORS_ORIGINS: list[str] = [
        "http://localhost:3000",
        "http://localhost:8000",
    ]

    @field_validator("BACKEND_CORS_ORIGINS", mode="before")
    @classmethod
    def assemble_cors_origins(cls, v: str | list[str]) -> list[str] | str:
        if isinstance(v, str) and not v.startswith("["):
            return [i.strip() for i in v.split(",")]
        elif isinstance(v, (list, str)):
            return v
        raise ValueError(v)

    # Database
    DATABASE_URL: str
    DATABASE_POOL_SIZE: int = 5
    DATABASE_MAX_OVERFLOW: int = 10

    # Redis
    REDIS_URL: str = "redis://localhost:6379/0"

    # Email (optional)
    SMTP_TLS: bool = True
    SMTP_PORT: int = 587
    SMTP_HOST: str | None = None
    SMTP_USER: str | None = None
    SMTP_PASSWORD: str | None = None
    EMAILS_FROM_EMAIL: EmailStr | None = None
    EMAILS_FROM_NAME: str | None = None

    # Termii SMS
    TERMII_API_KEY: str | None = None
    TERMII_SENDER_ID: str | None = "Timpbills"
    TERMII_BASE_URL: str = "https://api.ng.termii.com/api"

    # Paystack
    PAYSTACK_SECRET_KEY: str | None = None
    PAYSTACK_PUBLIC_KEY: str | None = None
    PAYSTACK_BASE_URL: str = "https://api.paystack.co"
    PAYSTACK_WEBHOOK_URL: str | None = None
    # URL Paystack redirects the user's browser to after a successful payment.
    # Doesn't need to resolve — the in-app WebView intercepts the URL change
    # and navigates to the FundingStatusPage. If None, Paystack falls back to
    # its own "/close" page which requires a manual tap.
    PAYSTACK_CALLBACK_URL: str = "https://timpbills.com/paystack/callback"

    # Paystack card-fee pass-through (no Timpbills margin on wallet funding per PRD §6.3).
    # Defaults match Paystack's published local-card fee structure:
    #   fee = amount * 1.5% + (₦100 if amount >= ₦2,500), capped at ₦2,000 total.
    PAYSTACK_CARD_FEE_PERCENT: float = 1.5
    PAYSTACK_CARD_FEE_FIXED_NAIRA: int = 100
    PAYSTACK_CARD_FEE_FIXED_THRESHOLD_NAIRA: int = 2500
    PAYSTACK_CARD_FEE_CAP_NAIRA: int = 2000

    # Resend Email
    RESEND_API_KEY: str | None = None
    EMAIL_FROM_ADDRESS: str = "noreply@timpbills.com"
    EMAIL_FROM_NAME: str = "Timpbills"

    # Force fake providers (useful for local dev without real keys)
    FORCE_FAKE_PROVIDERS: bool = False

    # ── VTPass (bill payments: airtime, data, electricity, cable) ────────
    # Sandbox: https://sandbox-api.vtpass.com — Prod: https://api.vtpass.com
    # The three keys are obtained from the VTPass dashboard; the webhook
    # secret is a shared secret we choose and configure on both sides
    # (VTPass does not HMAC-sign webhook bodies).
    VTPASS_API_KEY: str | None = None
    VTPASS_PUBLIC_KEY: str | None = None
    VTPASS_SECRET_KEY: str | None = None
    # VTPass docs (verified 2026-04-24):
    #   sandbox → https://sandbox.vtpass.com  (POST /api/pay, /api/requery)
    #   live    → https://vtpass.com
    # The `sandbox-api.vtpass.com` hostname a prior draft used does not
    # resolve — every VTPass call hung or silently failed, parking bills
    # in `processing` with no operator signal. Override this per-env in
    # .env if you need staging to point elsewhere.
    VTPASS_BASE_URL: str = "https://sandbox.vtpass.com"
    VTPASS_WEBHOOK_SECRET: str | None = None

    # ── Firebase Cloud Messaging (push notifications) ────────────────────
    # Either FCM_CREDENTIALS_PATH (service-account JSON file) OR
    # FCM_CREDENTIALS_JSON (inline base64/raw JSON) — one of them. When
    # both are unset the FakePushClient is used (tests + dev).
    FCM_CREDENTIALS_PATH: str | None = None
    FCM_CREDENTIALS_JSON: str | None = None
    FCM_PROJECT_ID: str = "timpbills"

    @field_validator("FCM_CREDENTIALS_JSON", mode="before")
    @classmethod
    def validate_fcm_credentials_json(cls, v: Any) -> str | None:
        """Sprint 4 B25: fail fast on malformed FCM_CREDENTIALS_JSON.

        The FCM client lazy-loads `json.loads(self._credentials_json)`
        inside `_get_access_token()`, so a malformed value would
        surface only at first push send — caught by the outer try/except
        in NotificationService._maybe_push and silently swallowed, with
        every push failing indefinitely and no startup signal to ops.

        Parse + validate at settings-load time instead: empty/None
        stays untouched (fake-push fallback path), non-empty must be
        valid JSON and must at minimum carry a `client_email` and
        `private_key` — the two fields google-auth actually needs.
        We don't validate private-key shape (Google's library owns
        that), just the structural envelope.
        """
        if v is None or v == "":
            return None
        if not isinstance(v, str):
            raise ValueError(
                f"FCM_CREDENTIALS_JSON must be a string, got {type(v).__name__}"
            )
        try:
            parsed = json.loads(v)
        except json.JSONDecodeError as e:
            raise ValueError(
                f"FCM_CREDENTIALS_JSON must be valid JSON: {e}"
            )
        if not isinstance(parsed, dict):
            raise ValueError(
                "FCM_CREDENTIALS_JSON must decode to a JSON object "
                "(service-account key), not an array or scalar"
            )
        missing = [k for k in ("client_email", "private_key") if not parsed.get(k)]
        if missing:
            raise ValueError(
                f"FCM_CREDENTIALS_JSON missing required service-account "
                f"fields: {missing}"
            )
        return v

    # ── Electricity purchase caps (Sprint 4) ─────────────────────────────
    # Max single-transaction amount (naira) for electricity purchases.
    # Applied per-DisCo via ELECTRICITY_DISCO_CAPS overrides; falls back to
    # ELECTRICITY_DEFAULT_CAP when a DisCo has no explicit entry.
    ELECTRICITY_DEFAULT_CAP: Decimal = Decimal("200000")
    # Per-DisCo overrides as a JSON dict parsed from env, e.g.
    #   ELECTRICITY_DISCO_CAPS='{"jos-electric": 50000}'
    # Values are in naira. Empty dict means "no overrides".
    ELECTRICITY_DISCO_CAPS: dict[str, Decimal] = {}

    @field_validator("ELECTRICITY_DISCO_CAPS", mode="before")
    @classmethod
    def parse_electricity_disco_caps(cls, v: Any) -> dict[str, Decimal]:
        if v is None or v == "":
            return {}
        if isinstance(v, dict):
            return {k: Decimal(str(val)) for k, val in v.items()}
        if isinstance(v, str):
            try:
                parsed = json.loads(v)
            except json.JSONDecodeError as e:
                raise ValueError(f"ELECTRICITY_DISCO_CAPS must be valid JSON: {e}")
            if not isinstance(parsed, dict):
                raise ValueError("ELECTRICITY_DISCO_CAPS must decode to a JSON object")
            return {k: Decimal(str(val)) for k, val in parsed.items()}
        raise ValueError(f"ELECTRICITY_DISCO_CAPS must be a JSON object, got {type(v).__name__}")

    # ── Cloudinary (avatar uploads — Sprint 5c · Task 3.1) ───────────────
    # All three required for live uploads; if any is missing the
    # AvatarService skips configuration and any upload attempt raises
    # AvatarUploadError up to the route, which returns 502. Tests patch
    # cloudinary.uploader.upload directly so real creds are never needed.
    CLOUDINARY_CLOUD_NAME: str | None = None
    CLOUDINARY_API_KEY: str | None = None
    CLOUDINARY_API_SECRET: str | None = None

    # ── Phase A+B auth migration ─────────────────────────────────────────
    # Flags + tunables for the phone-only-auth + PIN-login rollout. See
    # docs/plans/phone-only-auth.md for the staged migration timeline.
    AUTH_STRICT_GATES: bool = False
    """When True, protected endpoints refuse for users missing any of the
    three auth gates (email_verified, is_phone_verified, pin_hash). Flip
    to True after ~80% mobile-version adoption of the migration build."""

    AUTH_PIN_LOGIN_ENABLED: bool = True
    """Master switch for /auth/pin-login. Disable to temporarily force
    all users back to phone+password login."""

    TERMII_OTP_CHANNEL: str = "dnd"
    """Termii SMS channel for OTP delivery. ``dnd`` bypasses the NCC DND
    registry (essential for production OTP delivery on Nigerian carriers);
    ``generic`` is cheaper but blocked for DND-registered numbers."""

    OTP_RESEND_COOLDOWN_SECONDS: int = 60
    """Minimum seconds between successive OTP sends for the same
    (user, purpose) pair."""

    OTP_RESEND_DAILY_CAP: int = 10
    """Maximum OTPs per phone per day (covers all purposes combined).
    Defense against SMS-bombing of a single number."""

    # Observability — Sentry (optional; no-op when DSN unset)
    SENTRY_DSN: str | None = None
    SENTRY_ENVIRONMENT: str | None = None  # defaults to ENVIRONMENT if unset
    SENTRY_TRACES_SAMPLE_RATE: float = 0.05
    SENTRY_PROFILES_SAMPLE_RATE: float = 0.0

    # --- Admin dashboard auth (opaque session cookie) ---
    # NOTE: there is intentionally no FIRST_SUPERUSER_* setting. Admin users
    # are created out-of-band via `scripts/create_admin.py` (writes the
    # admin_users table directly). The old cookiecutter-template superuser
    # seeding was removed; keeping a required EmailStr here only ever broke
    # startup in contexts that don't need an admin (e.g. the live-API E2E
    # suite). Any leftover FIRST_SUPERUSER_* in a .env is ignored (extra="ignore").
    ADMIN_SESSION_TTL_SECONDS: int = 8 * 3600
    ADMIN_SESSION_COOKIE_NAME: str = "admin_session"
    ADMIN_CSRF_COOKIE_NAME: str = "admin_csrf"
    ADMIN_COOKIE_SECURE: bool = True
    ADMIN_COOKIE_DOMAIN: str | None = None  # set to ".timpbills.com" in staging/prod

    class Config:
        case_sensitive = True
        env_file = ".env"
        extra = "ignore"  # tolerate extra env vars so .env can document unused ones


settings = Settings()
