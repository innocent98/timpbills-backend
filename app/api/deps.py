from uuid import UUID

from fastapi import Depends, Header, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError
from redis.asyncio import Redis
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logger import log
from app.core.security import decode_token
from app.db.models.user import User
from app.db.session import SessionLocal
from app.integrations.base import SmsProvider
from app.integrations.email.base import EmailProvider
from app.integrations.email.fake import FakeEmailClient
from app.integrations.email.resend import ResendClient
from app.integrations.termii.client import TermiiClient
from app.integrations.termii.fake import FakeTermiiClient
from app.services.auth_service import AuthService
from app.services.token_store import RedisTokenStore, TokenStore

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login", auto_error=False)

# Singleton fake SMS client so tests can inspect .sent
_fake_sms_singleton = FakeTermiiClient()

# Singleton fake email client so tests can inspect .sent
_fake_email_singleton = FakeEmailClient()

# Singleton fake push client — tests inspect .sent; real FCM HTTP v1 is
# a Sprint 4 follow-up (see app/integrations/push/factory.py).
from app.integrations.push.factory import get_fake_singleton as _push_fake_singleton

_fake_push_singleton = _push_fake_singleton()

# Redis client singleton
_redis_client: Redis | None = None


def get_redis() -> Redis:
    global _redis_client
    if _redis_client is None:
        _redis_client = Redis.from_url(settings.REDIS_URL, decode_responses=True)
    return _redis_client


def get_token_store(redis: Redis = Depends(get_redis)) -> TokenStore:
    return RedisTokenStore(redis=redis)


def reset_fake_sms() -> None:
    """Clear the fake SMS singleton's sent messages (for test isolation)."""
    _fake_sms_singleton.sent.clear()


def reset_fake_email() -> None:
    """Clear the fake email singleton's sent messages (for test isolation)."""
    _fake_email_singleton.sent.clear()


def reset_fake_push() -> None:
    """Clear the fake push singleton's sent messages (for test isolation)."""
    from app.integrations.push import factory as _push_factory
    _push_factory.reset_fake_singleton()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_sms_provider() -> SmsProvider:
    env = getattr(settings, "ENVIRONMENT", "dev")
    if settings.FORCE_FAKE_PROVIDERS or env in ("dev", "test", "development"):
        return _fake_sms_singleton
    return TermiiClient()


def get_email_provider() -> EmailProvider:
    env = getattr(settings, "ENVIRONMENT", "dev")
    if settings.FORCE_FAKE_PROVIDERS or env in ("dev", "test", "development"):
        return _fake_email_singleton
    return ResendClient()


def get_auth_service(
    db: Session = Depends(get_db),
    sms: SmsProvider = Depends(get_sms_provider),
    email: EmailProvider = Depends(get_email_provider),
    token_store: TokenStore = Depends(get_token_store),
    redis: Redis = Depends(get_redis),
) -> AuthService:
    # Lazy import to avoid pulling redis-asyncio types at module-import
    # time in callers that only need other helpers from this module.
    from app.services.token_revocation_service import TokenRevocationService

    return AuthService(
        db=db,
        sms=sms,
        email=email,
        token_store=token_store,
        revocation_svc=TokenRevocationService(redis=redis),
        redis=redis,
    )


async def get_current_token_claims(
    token: str | None = Depends(oauth2_scheme),
    redis: Redis = Depends(get_redis),
) -> dict:
    """Decode the bearer token + enforce the JWT-jti blocklist.

    Shared between ``get_current_user`` (the per-request auth gate) and
    ``/auth/logout`` (which needs the raw claims to know which jti to
    revoke). Centralising here means the blocklist check happens in
    exactly one place — adding more revocation hooks later (e.g.
    password reset → revoke all access tokens) only touches one site.

    Refresh-typed tokens are rejected: refresh tokens have a separate
    lifecycle and must hit ``/auth/refresh`` rather than any
    bearer-protected endpoint.

    Note: the ``tokens_revoked_at`` "logged-out-everywhere" check lives
    in ``get_current_user`` rather than here — it requires the User row
    which is loaded only one level deeper. /auth/logout uses
    ``get_current_token_claims`` (no user row needed) and is allowed
    to succeed even with a revoked-everywhere token: the worst case is
    a no-op revocation of an already-revoked jti.
    """
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "UNAUTHORIZED", "message": "Missing credentials"},
        )
    try:
        payload = decode_token(token)
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "INVALID_TOKEN", "message": "Invalid or expired token"},
        )
    if payload.get("typ") == "refresh":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "INVALID_TOKEN", "message": "Refresh token cannot be used as access token"},
        )
    jti = payload.get("jti")
    if jti:
        # Lazy import — avoids circular import in test bootstrap.
        from app.services.token_revocation_service import TokenRevocationService

        svc = TokenRevocationService(redis=redis)
        if await svc.is_revoked(jti):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"code": "TOKEN_REVOKED", "message": "Token has been revoked"},
            )
    return payload


def _token_iat_predates_revocation(payload: dict, revoked_at) -> bool:
    """Return True iff the JWT's ``iat`` is at-or-before the user's
    ``tokens_revoked_at`` stamp.

    Both values are normalised to integer unix-epoch seconds before
    comparison. Python's datetime cmp would also work, but jose hands
    us ``iat`` as either an int (newer jose) or a datetime (older
    jose), so we normalise once and avoid the polymorphism.

    The comparison is ``<=``, not ``<``: a token issued in the *same
    wall-clock second* as the revocation stamp must be treated as
    revoked. Otherwise a token minted in the same second as a password
    change (e.g. by a clock-skewed mobile retry, or by a test like
    ours where issue+change happen in microseconds) would slip
    through with ``iat_unix == revoked_unix`` and ``iat < revoked``
    being false."""
    from datetime import UTC, datetime

    if revoked_at is None:
        return False
    iat = payload.get("iat")
    if iat is None:
        # No iat claim — be defensive and reject so a token without
        # provenance can't slip past a global revocation.
        return True
    if isinstance(iat, datetime):
        iat_unix = int(iat.timestamp())
    else:
        iat_unix = int(iat)
    revoked_at_aware = (
        revoked_at if revoked_at.tzinfo is not None else revoked_at.replace(tzinfo=UTC)
    )
    revoked_unix = int(revoked_at_aware.timestamp())
    return iat_unix <= revoked_unix


async def get_current_user(
    payload: dict = Depends(get_current_token_claims),
    db: Session = Depends(get_db),
) -> User:
    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "INVALID_TOKEN", "message": "Invalid token"},
        )
    try:
        user_uuid = UUID(user_id)
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "INVALID_TOKEN", "message": "Invalid token"},
        )
    user = db.query(User).filter(User.id == user_uuid).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "USER_NOT_FOUND", "message": "User not found"},
        )
    # Task 4.2: enforce "log me out everywhere". If the user's
    # ``tokens_revoked_at`` postdates this token's ``iat``, reject —
    # whether or not the specific jti is in the blocklist. Catches
    # tokens that were issued before a password change and whose
    # access TTL hasn't yet expired on its own.
    if _token_iat_predates_revocation(payload, user.tokens_revoked_at):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "TOKEN_REVOKED", "message": "Token has been revoked"},
        )
    # Task 6.1: a soft-deleted (or otherwise deactivated) user must not
    # be able to keep using a still-valid access token. The delete
    # endpoint also stamps ``tokens_revoked_at`` so the previous branch
    # catches the common case; this branch defends against a deactivated
    # user whose ``tokens_revoked_at`` somehow wasn't stamped (e.g. an
    # admin flips ``is_active`` directly in Sprint 8).
    if user.is_active is False:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "ACCOUNT_DISABLED", "message": "Account is disabled"},
        )
    return user


def require_admin(user: User = Depends(get_current_user)) -> User:
    """Authorize an admin-only endpoint.

    Builds on top of `get_current_user` (so the JWT auth + token-type
    checks still run) and additionally requires `user.is_admin`. We
    return 403 (not 404) on a non-admin: the user is authenticated and
    the route exists; they're just not allowed.

    Sprint 5 BE-52 — manual-refund endpoint. Sprint 8 will fold the
    full admin dashboard surface in here; the dependency stays generic
    so other admin endpoints can opt in by name.
    """
    if not user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "ADMIN_REQUIRED", "message": "Admin privilege required"},
        )
    return user


# --- Added by B5 (IdempotencyService + header guard) ---
from app.services.idempotency_service import IdempotencyService


def get_idempotency_service(redis: Redis = Depends(get_redis)) -> IdempotencyService:
    return IdempotencyService(redis=redis)


def require_idempotency_key(
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> str:
    if not idempotency_key:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "IDEMPOTENCY_KEY_REQUIRED",
                "message": "Idempotency-Key header required for this endpoint",
            },
        )
    return idempotency_key


# --- Added by B4 (PinService + pin token guard) ---
from app.services.pin_service import PinService


def get_pin_service(
    db: Session = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> PinService:
    return PinService(db=db, redis=redis)


def require_pin_token(
    x_pin_token: str | None = Header(default=None, alias="X-Pin-Token"),
    user: User = Depends(get_current_user),
) -> str:
    if not x_pin_token:
        raise HTTPException(
            status_code=401,
            detail={"code": "PIN_TOKEN_REQUIRED", "message": "Missing X-Pin-Token"},
        )
    try:
        payload = decode_token(x_pin_token)
    except JWTError:
        raise HTTPException(
            status_code=401,
            detail={"code": "INVALID_PIN_TOKEN", "message": "Invalid or expired PIN token"},
        )
    if payload.get("scope") != "money-ops" or payload.get("sub") != str(user.id):
        raise HTTPException(
            status_code=401,
            detail={"code": "INVALID_PIN_TOKEN", "message": "PIN token scope mismatch"},
        )
    return x_pin_token


# --- Added by B6 (Paystack provider) ---
from app.integrations.paystack import factory as _paystack_factory
from app.integrations.paystack.base import PaymentProvider


def get_paystack_provider() -> PaymentProvider:
    return _paystack_factory.select_paystack_client()


def reset_fake_paystack() -> None:
    _paystack_factory.reset_fake_singleton()


# Back-compat shim for tests that import the singleton directly.
def __getattr__(name: str):  # pragma: no cover - import plumbing
    if name == "_fake_paystack_singleton":
        return _paystack_factory.get_fake_singleton()
    raise AttributeError(name)


# --- Added by B7 (wallet + transaction services) ---
from app.services.transaction_service import TransactionService
from app.services.wallet_service import WalletService


def get_wallet_service(db: Session = Depends(get_db)) -> WalletService:
    return WalletService(db=db)


def get_transaction_service(db: Session = Depends(get_db)) -> TransactionService:
    return TransactionService(db=db)


# --- Sprint 3 · B4+B6: VTPass provider + BillService ---
from app.integrations.vtpass import factory as _vtpass_factory
from app.integrations.vtpass.base import BillProvider
from app.services.bill_service import BillService


def get_vtpass_provider() -> BillProvider:
    return _vtpass_factory.select_vtpass_client()


def reset_fake_vtpass() -> None:
    _vtpass_factory.reset_fake_singleton()


def get_bill_service(
    db: Session = Depends(get_db),
    tx_svc: TransactionService = Depends(get_transaction_service),
    wallet_svc: WalletService = Depends(get_wallet_service),
    provider: BillProvider = Depends(get_vtpass_provider),
    redis: Redis = Depends(get_redis),
) -> BillService:
    return BillService(
        db=db, tx_svc=tx_svc, wallet_svc=wallet_svc, provider=provider,
        redis=redis,
    )


# --- Sprint 4 · B15+B16: PushTokensService ---
from app.services.push_tokens_service import PushTokensService


def get_push_tokens_service(db: Session = Depends(get_db)) -> PushTokensService:
    return PushTokensService(db=db)


# --- Sprint 5c · Task 3.2: AvatarService ---
from app.services.avatar_service import AvatarService


def get_avatar_service() -> AvatarService:
    return AvatarService(
        cloud_name=settings.CLOUDINARY_CLOUD_NAME,
        api_key=settings.CLOUDINARY_API_KEY,
        api_secret=settings.CLOUDINARY_API_SECRET,
    )


# --- Sprint 5c · Task 3.3: TokenRevocationService ---
from app.services.token_revocation_service import TokenRevocationService


def get_token_revocation_service(
    redis: Redis = Depends(get_redis),
) -> TokenRevocationService:
    return TokenRevocationService(redis=redis)


# --- Sprint 5d · B7: phone-only-auth gate enforcement ---
def require_full_auth_gates(user: User = Depends(get_current_user)) -> User:
    """Reject (403) any protected endpoint when the user has not yet passed
    all three auth gates: email_verified, is_phone_verified, pin_hash set.

    Soft mode (``AUTH_STRICT_GATES = False``): logs a warning + lets the
    request through. Use during the rollout window while mobile catches
    up to the new flows.
    Strict mode (``AUTH_STRICT_GATES = True``): hard reject with a
    ``VERIFICATION_REQUIRED`` 403 carrying ``which`` to tell mobile
    which gate failed.
    """
    missing: str | None = None
    if not user.email_verified:
        missing = "email"
    elif not user.is_phone_verified:
        missing = "phone"
    elif user.pin_hash is None:
        missing = "pin_setup"

    if missing is None:
        return user

    if settings.AUTH_STRICT_GATES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "VERIFICATION_REQUIRED",
                "message": f"{missing} verification required",
                "which": missing,
            },
        )
    log.warning(
        "auth_gates: user %s missing %s but soft mode active — request allowed",
        user.id, missing,
    )
    return user
