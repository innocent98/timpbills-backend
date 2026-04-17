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

    # Wallet fees (% of funded amount; cap in naira)
    WALLET_FUNDING_FEE_PERCENT: float = 1.0
    WALLET_FUNDING_FEE_CAP_NAIRA: int = 500

    # Resend Email
    RESEND_API_KEY: Optional[str] = None
    EMAIL_FROM_ADDRESS: str = "noreply@timpbills.com"
    EMAIL_FROM_NAME: str = "Timpbills"

    # Force fake providers (useful for local dev without real keys)
    FORCE_FAKE_PROVIDERS: bool = False

    # Admin
    FIRST_SUPERUSER_EMAIL: EmailStr
    FIRST_SUPERUSER_PASSWORD: str

    class Config:
        case_sensitive = True
        env_file = ".env"
        extra = "ignore"  # tolerate extra env vars so .env can document unused ones


settings = Settings()
