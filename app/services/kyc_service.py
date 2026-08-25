# app/services/kyc_service.py
"""KycService — core KYC orchestration (spec §3.2).

`start_verification` mints a pending KycRecord + backend-owned reference;
`confirm_verification` fetches Dojah's authoritative result for that
reference, validates it, and upgrades the user's tier on pass. Both the
`/kyc/verify/confirm` endpoint and the `/kyc/webhook` handler (A7) call
`confirm_verification` — the row lock below is what makes that safe to
race.

Never persist raw PII: only `masked_id` (last 2 digits, from the provider
result) lands on the record.
"""
import secrets
from datetime import date
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logger import log
from app.db.models.kyc_record import KycRecord
from app.db.models.user import KycLevel, User
from app.integrations.dojah.factory import get_kyc_provider
from app.integrations.dojah.schemas import KycVerificationResult


class KycTierPrecondition(Exception):
    """User's current kyc_level doesn't match what this verification_type
    requires (e.g. NIN attempted before BVN has been passed)."""


class DobRequired(Exception):
    """Neither the user record nor the request carries a date_of_birth."""


class UnknownReference(Exception):
    """No KycRecord exists for the given provider_reference."""


class KycProviderError(Exception):
    """The KYC provider call failed (network/5xx). The record is left
    `pending` — safe to retry via /kyc/status or the webhook backstop."""


# verification_type -> the KycLevel required to START that step.
_TIER_REQUIRED_FOR_START: dict[str, KycLevel] = {
    "bvn": KycLevel.tier_1,
    "nin": KycLevel.tier_2,
}

# verification_type -> the KycLevel a PASS upgrades the user to. Keyed by
# verification_type (not "current tier + 1") so a stray re-application (a
# bug in the idempotency guard, say) can never double-bump past the
# type's own ceiling.
_TIER_AFTER_PASS: dict[str, KycLevel] = {
    "bvn": KycLevel.tier_2,
    "nin": KycLevel.tier_3,
}


def _notify_kyc_verification_result(*, user: User, record: KycRecord) -> None:
    """Fire-and-forget email + push for a KYC verification that just
    resolved out of pending. Callers MUST gate this on `was_pending`
    (only call when the record was pending before this confirm applied
    a result) so a verification is notified exactly once no matter how
    many callers — /verify/confirm, /status polling, the webhook — race
    to resolve the same reference.

    Unlike BillService's notify helpers (called from a context where the
    tx is already committed and the caller has no further work to do),
    a KYC confirm's return value IS the API response body — so on top of
    NotificationService's own per-channel try/except, we wrap the whole
    dispatch here too. A notification hiccup must never turn a
    successful KYC confirm into a 5xx.
    """
    from app.services.notification_service import (  # noqa: PLC0415
        NotificationEvent,
        build_kyc_context,
    )
    from app.services.wallet_service import _resolve_cap  # noqa: PLC0415
    from app.workers.tasks.notification_tasks import dispatch_delay  # noqa: PLC0415

    try:
        if record.status == "success":
            # Report the user's ACTUAL live tier/cap, not record.tier_after —
            # a stale/duplicate pending record confirmed late (see
            # test_confirm_pass_never_downgrades_user_already_at_higher_tier)
            # can carry a lower tier_after than the user has already reached;
            # notifying with that stale tier would be flat wrong.
            cap = _resolve_cap(user.kyc_level)
            wallet_cap_label = "Unlimited" if cap is None else f"₦{cap:,.0f}"
            ctx = build_kyc_context(
                verification_type=record.verification_type,
                status="success",
                tier=user.kyc_level.numeric,
                wallet_cap_label=wallet_cap_label,
            )
            dispatch_delay(
                user_id=str(user.id), user_email=user.email,
                event=NotificationEvent.kyc_verification_success, context=ctx,
            )
        elif record.status == "failed":
            ctx = build_kyc_context(
                verification_type=record.verification_type, status="failed",
            )
            dispatch_delay(
                user_id=str(user.id), user_email=user.email,
                event=NotificationEvent.kyc_verification_failed, context=ctx,
            )
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "kyc notify: dispatch failed ref=%s status=%s err=%s",
            record.provider_reference, record.status, exc,
        )


class KycService:
    def __init__(self, *, db: Session) -> None:
        self._db = db

    def start_verification(
        self,
        *,
        user: User,
        verification_type: str,
        date_of_birth: date | None = None,
    ) -> str:
        """Tier-gate, resolve DOB, mint a reference, insert a pending
        KycRecord. Returns the minted reference_id."""
        if verification_type not in _TIER_REQUIRED_FOR_START:
            raise ValueError(
                f"unsupported verification_type: {verification_type!r}"
            )

        required_tier = _TIER_REQUIRED_FOR_START[verification_type]
        if user.kyc_level != required_tier:
            raise KycTierPrecondition(
                f"{verification_type} requires kyc_level={required_tier.value}, "
                f"user is {user.kyc_level.value}"
            )

        if user.date_of_birth is None:
            if date_of_birth is None:
                raise DobRequired()
            user.date_of_birth = date_of_birth
            self._db.flush()

        reference_id = f"KYC-{verification_type.upper()}-{secrets.token_hex(8)}"

        record = KycRecord(
            user_id=user.id,
            verification_type=verification_type,
            provider="dojah",
            provider_reference=reference_id,
            status="pending",
            tier_before=user.kyc_level.numeric,
            masked_id=None,
        )
        self._db.add(record)
        self._db.commit()
        return reference_id

    async def confirm_verification(
        self, *, reference_id: str, source: str = "api",
        expected_user_id: UUID | None = None,
    ) -> KycRecord:
        """Fetch the provider's authoritative result and apply it.

        Idempotent: a record already `success` is returned unchanged (no
        re-fetch, no re-upgrade) — this is what lets the api-confirm and
        the webhook race safely on the same reference.

        The row lock is deliberately NOT held across the Dojah HTTP call:
        we look the record up unlocked, do the (potentially slow) network
        round-trip, and only THEN take `.with_for_update()` to persist the
        result — holding a Postgres row lock for the duration of an
        external HTTP call would serialize every other reader of that row
        (e.g. `/kyc/status` polling) behind Dojah's latency. Because the
        lock is acquired after the fetch, a concurrent caller (the other
        of api-confirm/webhook) may have already applied success while we
        were awaiting the provider — we re-check `status == "success"`
        immediately after re-acquiring the lock and discard our own
        (now-stale) fetch result in that case, so only one caller's result
        ever gets applied.

        ``expected_user_id`` lets the authenticated `/kyc/verify/confirm`
        endpoint (A7) enforce ownership: a reference that exists but
        belongs to a different user is treated identically to an unknown
        reference, so the API never confirms — or leaks the existence of
        — another user's verification. The webhook has no authenticated
        user to compare against and passes ``None``.
        """
        record = self._db.execute(
            select(KycRecord).where(KycRecord.provider_reference == reference_id)
        ).scalar_one_or_none()
        if record is None:
            raise UnknownReference(reference_id)
        if expected_user_id is not None and record.user_id != expected_user_id:
            raise UnknownReference(reference_id)

        was_pending = record.status == "pending"

        if record.status == "success":
            return record

        try:
            result = await get_kyc_provider().fetch_verification(
                reference_id=reference_id,
            )
        except Exception as exc:
            log.error(
                "confirm_verification: provider error ref=%s source=%s: %s",
                reference_id, source, exc,
            )
            raise KycProviderError(str(exc)) from exc

        # Only now take the row lock — the network call above ran with no
        # lock held. Re-check under the lock: a concurrent caller may have
        # already applied success while we were awaiting the provider.
        record = self._db.execute(
            select(KycRecord)
            .where(KycRecord.provider_reference == reference_id)
            .with_for_update()
        ).scalar_one_or_none()
        if record is None:
            raise UnknownReference(reference_id)
        if record.status == "success":
            return record

        # Persist the component fields regardless of outcome — pending
        # results still carry (empty) liveness/face/masked_id state, and
        # a later poll/webhook needs the freshest values on the record.
        record.liveness_passed = result.liveness_passed
        record.face_match = result.face_match
        record.face_match_confidence = result.face_match_confidence
        record.masked_id = result.masked_id

        if result.status == "pending":
            self._db.commit()
            return record

        user = self._db.query(User).filter(User.id == record.user_id).first()

        failure_reason = self._first_failure_reason(
            result=result, record=record, user=user,
        )

        if failure_reason is not None:
            record.status = "failed"
            record.failure_reason = failure_reason
            self._db.commit()
            if was_pending and user is not None:
                _notify_kyc_verification_result(user=user, record=record)
            return record

        next_tier = _TIER_AFTER_PASS[record.verification_type]
        record.status = "success"
        record.tier_after = next_tier.numeric
        record.failure_reason = None
        # Only ever upgrade — never apply a lower/equal tier over a user
        # who has already progressed past it (e.g. a stale/duplicate
        # PENDING record confirmed late, after a webhook or a fresh
        # verification already advanced the user further). The audit row
        # above still records the pass regardless.
        upgraded = user is not None and next_tier.numeric > user.kyc_level.numeric
        if upgraded:
            user.kyc_level = next_tier
        self._db.commit()
        # Over-cap spend-lock unlock (DVA): a tier upgrade may now cover a
        # balance that landed over the old cap. Clear the lock iff the new
        # cap covers the balance; leave it locked otherwise. Never mutate the
        # wallet outside WalletService.
        if upgraded and user is not None:
            from app.services.wallet_service import WalletService  # noqa: PLC0415
            WalletService(db=self._db).clear_spend_lock_if_within_cap(user_id=user.id)
        if was_pending and user is not None:
            _notify_kyc_verification_result(user=user, record=record)
        return record

    @staticmethod
    def _first_failure_reason(
        *, result: KycVerificationResult, record: KycRecord, user: User | None,
    ) -> str | None:
        """First failing check, in spec priority order: id -> liveness ->
        face -> identity_dob. `result.status == "pending"` is handled by
        the caller before this is reached.

        `result.status != "success"` or a verification_type mismatch with
        every boolean field true is not exercised by any real provider
        path (the fake always keeps status consistent with the booleans,
        and verification_type is derived from the same reference_id the
        record was minted with) — defensively bucketed under
        "id_not_verified" so a record is never left without a reason.
        """
        if not result.id_verified:
            return "id_not_verified"
        if not result.liveness_passed:
            return "liveness_failed"
        if not result.face_match:
            return "face_mismatch"
        if (
            result.identity_dob is not None
            and user is not None
            and user.date_of_birth is not None
            and result.identity_dob != user.date_of_birth
        ):
            return "identity_mismatch"
        if result.status != "success" or result.verification_type != record.verification_type:
            return "id_not_verified"
        return None
