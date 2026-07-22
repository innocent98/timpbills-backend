"""VirtualAccountService — provision a Paystack Dedicated Virtual Account.

Single-step assign. The BVN + bank account supplied at setup are passed
straight to Paystack and never persisted; we store only Paystack's durable
customer_code (obtained from create_customer, idempotent by email) plus the
issued account details written later by the assign webhook.

Provisioning is idempotent: an existing active/pending row is returned as-is
with no second Paystack call. A prior `failed`/`deactivated` row is reused and
reset to `pending_identity` on retry (mobile shows a Retry button).
"""
from uuid import UUID

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logger import log
from app.db.models._enums import VirtualAccountStatus
from app.db.models.user import User
from app.db.models.virtual_account import VirtualAccount
from app.integrations.paystack.base import PaymentProvider

# Statuses where a DVA already exists and must NOT be re-provisioned.
_LIVE_STATUSES = frozenset(
    {
        VirtualAccountStatus.pending_identity,
        VirtualAccountStatus.pending_assign,
        VirtualAccountStatus.active,
    }
)


class KycRequired(Exception):
    """User's KYC tier is below the tier-1 gate for provisioning a DVA."""


def split_full_name(full_name: str) -> tuple[str, str, str]:
    """Split our single ``full_name`` into (first, middle, last).

    First token -> first_name, last token -> last_name, everything between ->
    middle_name (empty string for two tokens). A single token sets
    ``last_name = first_name`` as a fallback and logs a warning.
    """
    tokens = full_name.split()
    if not tokens:
        return ("", "", "")
    if len(tokens) == 1:
        log.warning("dva: single-token full_name %r; using it for last_name too", full_name)
        return (tokens[0], "", tokens[0])
    first = tokens[0]
    last = tokens[-1]
    middle = " ".join(tokens[1:-1])
    return (first, middle, last)


class VirtualAccountService:
    def __init__(self, *, db: Session, paystack: PaymentProvider) -> None:
        self._db = db
        self._paystack = paystack

    def get_for_user(self, *, user_id: UUID) -> VirtualAccount | None:
        return (
            self._db.query(VirtualAccount)
            .filter(VirtualAccount.user_id == user_id)
            .first()
        )

    async def provision(
        self,
        *,
        user: User,
        bvn: str,
        account_number: str,
        bank_code: str,
        preferred_bank: str | None = None,
    ) -> VirtualAccount:
        if user.kyc_level.numeric < 1:
            raise KycRequired()

        existing = self.get_for_user(user_id=user.id)
        if existing is not None and existing.status in _LIVE_STATUSES:
            return existing  # idempotent: no second Paystack call

        first, middle, last = split_full_name(user.full_name)

        customer = await self._paystack.create_customer(
            email=user.email, first_name=first, last_name=last, phone=user.phone,
        )

        if existing is not None:
            va = existing  # retry over a failed/deactivated row
            va.paystack_customer_code = customer.customer_code
            va.paystack_customer_id = customer.customer_id
            va.status = VirtualAccountStatus.pending_identity
            va.failure_reason = None
        else:
            va = VirtualAccount(
                user_id=user.id,
                paystack_customer_code=customer.customer_code,
                paystack_customer_id=customer.customer_id,
                status=VirtualAccountStatus.pending_identity,
                currency="NGN",
            )
            self._db.add(va)
        self._db.commit()

        # 202 — result arrives via customeridentification.* then
        # dedicatedaccount.assign.* webhooks. BVN is never persisted.
        await self._paystack.assign_dedicated_account(
            email=user.email,
            first_name=first,
            middle_name=middle,
            last_name=last,
            phone=user.phone,
            preferred_bank=preferred_bank or settings.PAYSTACK_DVA_PREFERRED_BANK,
            country="NG",
            account_number=account_number,
            bvn=bvn,
            bank_code=bank_code,
        )
        self._db.refresh(va)
        return va
