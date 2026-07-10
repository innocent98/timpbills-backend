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
    ) -> KycRecord:
        """Fetch the provider's authoritative result and apply it.

        Idempotent: a record already `success` is returned unchanged (no
        re-fetch, no re-upgrade) — this is what lets the api-confirm and
        the webhook race safely on the same reference. The row lock below
        serializes the two callers so only one of them applies the
        upgrade.
        """
        record = self._db.execute(
            select(KycRecord)
            .where(KycRecord.provider_reference == reference_id)
            .with_for_update()
        ).scalar_one_or_none()
        if record is None:
            raise UnknownReference(reference_id)

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
            return record

        next_tier = _TIER_AFTER_PASS[record.verification_type]
        record.status = "success"
        record.tier_after = next_tier.numeric
        record.failure_reason = None
        if user is not None:
            user.kyc_level = next_tier
        self._db.commit()
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
