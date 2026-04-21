from typing import List, Optional
from pydantic import AnyHttpUrl, field_validator, EmailStr
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
    BACKEND_CORS_ORIGINS: List[str] = [
        "http://localhost:3000",
        "http://localhost:8000",
    ]

    @field_validator("BACKEND_CORS_ORIGINS", mode="before")
    @classmethod
    def assemble_cors_origins(cls, v: str | List[str]) -> List[str] | str:
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
    SMTP_HOST: Optional[str] = None
    SMTP_USER: Optional[str] = None
    SMTP_PASSWORD: Optional[str] = None
    EMAILS_FROM_EMAIL: Optional[EmailStr] = None
    EMAILS_FROM_NAME: Optional[str] = None

    # Termii SMS
    TERMII_API_KEY: Optional[str] = None
    TERMII_SENDER_ID: Optional[str] = "Timpbills"
    TERMII_BASE_URL: str = "https://api.ng.termii.com/api"

    # Paystack
    PAYSTACK_SECRET_KEY: Optional[str] = None
    PAYSTACK_PUBLIC_KEY: Optional[str] = None
    PAYSTACK_BASE_URL: str = "https://api.paystack.co"
    PAYSTACK_WEBHOOK_URL: Optional[str] = None
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
    RESEND_API_KEY: Optional[str] = None
    EMAIL_FROM_ADDRESS: str = "noreply@timpbills.com"
    EMAIL_FROM_NAME: str = "Timpbills"

    # Force fake providers (useful for local dev without real keys)
    FORCE_FAKE_PROVIDERS: bool = False

    # ── VTPass (bill payments: airtime, data, electricity, cable) ────────
    # Sandbox: https://sandbox-api.vtpass.com — Prod: https://api.vtpass.com
    # The three keys are obtained from the VTPass dashboard; the webhook
    # secret is a shared secret we choose and configure on both sides
    # (VTPass does not HMAC-sign webhook bodies).
    VTPASS_API_KEY: Optional[str] = None
    VTPASS_PUBLIC_KEY: Optional[str] = None
    VTPASS_SECRET_KEY: Optional[str] = None
    VTPASS_BASE_URL: str = "https://sandbox-api.vtpass.com"
    VTPASS_WEBHOOK_SECRET: Optional[str] = None

    # ── Firebase Cloud Messaging (push notifications) ────────────────────
    # Either FCM_CREDENTIALS_PATH (service-account JSON file) OR
    # FCM_CREDENTIALS_JSON (inline base64/raw JSON) — one of them. When
    # both are unset the FakePushClient is used (tests + dev).
    FCM_CREDENTIALS_PATH: Optional[str] = None
    FCM_CREDENTIALS_JSON: Optional[str] = None
    FCM_PROJECT_ID: Optional[str] = None

    # Observability — Sentry (optional; no-op when DSN unset)
    SENTRY_DSN: Optional[str] = None
    SENTRY_ENVIRONMENT: Optional[str] = None  # defaults to ENVIRONMENT if unset
    SENTRY_TRACES_SAMPLE_RATE: float = 0.05
    SENTRY_PROFILES_SAMPLE_RATE: float = 0.0

    # Admin
    FIRST_SUPERUSER_EMAIL: EmailStr
    FIRST_SUPERUSER_PASSWORD: str

    class Config:
        case_sensitive = True
        env_file = ".env"
        extra = "ignore"  # tolerate extra env vars so .env can document unused ones


settings = Settings()
