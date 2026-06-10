# Transaction detail page parity — design

**Date:** 2026-06-10
**Scope:** Cross-stack — `timpbills-backend` (txn detail user payload) + `timpbills-marketing` (`/platform-admin/transactions/[ref]`).
**Status:** Approved, pending implementation plan.

## Goal

Bring the admin transaction-detail page to parity with the `04 _ Transaction detail.png`
handoff, within the constraint that some mock fields are not captured in the data:

1. **Refund panel** (right column) with a manual-refund trigger + requery — the
   highest-value gap (the backend `POST /admin/refunds/{ref}/trigger` already exists).
2. **Customer card** enriched with wallet balance + member-since (needs a small
   backend addition).
3. **Hero contextual banner** (status-driven, real event copy).
4. **Transaction data** surfacing real fields only.

### Explicitly out of scope (data not captured — no fabrication)
Meter/smartcard number, channel/device (e.g. "Mobile · iOS 17.4"), and a distinct
idempotency-key field are **not stored** in `transactions.meta` (which currently holds
only `shortfall_ngn`, `partial_delivery`, `delivered_amount_ngn`, `vtpass_transaction_id`).
These mock fields are omitted, not invented. Capturing them would be a separate
mobile + backend change.

## Background (verified against live code + DB)

- Refunds are modelled as separate `type=refund` transactions; the manual-refund
  endpoint (`admin.py:88` `admin_trigger_refund`) accepts any bill type, **rejects**
  `refund`/`wallet_funding` with 400 `UNREFUNDABLE_TX_TYPE`, is idempotent
  (`was_created=false` on repeat), credits the wallet, and walks the original
  `success`/`failed` → `refund_pending` → `refunded` on a fresh refund.
- `ManualRefundRequest.reason` is **mandatory**, `min_length=3, max_length=500`.
- The current `triggerRefund` client method is **mistyped** — declared to return
  `RefundItem`, but the endpoint returns
  `{transaction_reference, transaction_status, refund_reference, refund_amount, was_created}`.
- `get_transaction_detail` returns a `user` payload of
  `{id, full_name, email, phone, kyc_tier}` — no wallet balance, no created_at.
- `get_user_detail`/`list_users` already format wallet balance as
  `f"{(w.balance if w else 0):.2f}"` via an outer-joined `Wallet` — mirror that.

## Backend — `app/services/admin_service.py`

In `get_transaction_detail`, extend the `user` dict with:
- `wallet_balance`: query the user's `Wallet` (single row by `user_id`), format
  `f"{(wallet.balance if wallet else 0):.2f}"`.
- `created_at`: `user.created_at.isoformat()`.

No change to the `payment`/`events`/top-level shape.

**Tests** (`tests/services/test_admin_transaction_detail*` / `tests/api/...`): assert the
`user` payload now includes `wallet_balance` (string) and `created_at` (ISO string),
including the no-wallet case → `"0.00"`.

## Frontend — `lib/admin-api.ts`

- `TxnDetailUser`: add `wallet_balance: string;` and `created_at: string;`.
- Add `RefundTriggerResult` and fix the method's return type:
  ```ts
  export interface RefundTriggerResult {
    transaction_reference: string;
    transaction_status: string;
    refund_reference: string;
    refund_amount: string;   // money string
    was_created: boolean;
  }
  ```
  Change `triggerRefund` to `call<RefundTriggerResult>(...)`.

## Frontend — `app/platform-admin/transactions/[ref]/page.tsx`

### 1. Right column → "Refund" card (replaces the "Status" card)
Eligibility, derived from `t.type` + `t.status`:
- **Requeryable** (`pending`/`processing`): keep the existing Requery action.
- **Refundable** (type ∉ {`refund`, `wallet_funding`} AND status ∈ {`failed`, `success`}
  AND status ≠ `refunded`): show
  - a warning-tint explanation,
  - an **inline reason `<input>`** (`maxLength={500}`, placeholder
    "Reason for manual refund"),
  - a primary **"Trigger manual refund"** button → `adminApi.triggerRefund(ref, reason)`
    where `reason` falls back to `"Manual refund via admin console"` when the input is
    blank (always satisfies the backend `min_length=3`),
  - a secondary **"Requery provider"** button.
- **Settled / already refunded / non-refundable**: explanatory state, no actions.
- On refund response: show feedback keyed on `was_created`
  (`true` → "Refund issued · ₦{refund_amount} credited to wallet";
  `false` → "A refund already exists for this transaction — no action taken"),
  then `reload()`. On error, show `errorInfo(e).message`.

### 2. Hero contextual banner
A status-driven badge near the hero pills:
- `failed` → "Service delivery failed"; `refunded` → "Refunded"; `refund_pending` →
  "Refund in progress"; else omit.
- Sub-text: the `reason` of the most recent `failed`/refund event from `t.events`
  when present (real data); otherwise no sub-text. Never fabricated copy.

### 3. Customer card
Add two rows from the new backend fields, alongside Phone + KYC tier:
- **Wallet balance**: `<Naira/>` + `fmtMoney(t.user.wallet_balance)`.
- **Member since**: month + year from `t.user.created_at` (e.g. "Feb 2026").

### 4. Transaction data
Keep the existing real fields (Type, Status, Fee, Reference, Provider, Provider ref,
Payment method, Payment status). Additionally render present, meaningful `meta` keys
when available (e.g. `vtpass_transaction_id`). Omit fields not in the data.

## Reused, not rebuilt
Primitives `Card`, `Naira`, `TypePill`, `StatusPill`, `Avatar`, `TierTag`, `fmtMoney`;
`Timeline`; `states` helpers `initials`, `fmtDateTime`, `errorInfo`, `useAdminResource`;
the existing requery handler.

## Verification
- Backend: `pytest` for the new user-payload fields (with + without a wallet row);
  full suite + ruff green.
- Frontend: `npx next build` green.
- Manual (backend up): open a failed bill txn → trigger manual refund with a reason →
  see wallet credited + status walk to refunded on reload; confirm Customer card shows
  wallet balance + member since.

## Risks / notes
- `triggerRefund` retype is a type-correctness fix (the old `RefundItem` type never
  matched the endpoint). One other caller exists — `refunds/page.tsx:92` — but it
  `await`s the call and **ignores the return value**, so the retype is safe and contained.
- Manual refund is a **write** → goes through the CSRF double-submit header the client
  already attaches on non-GET calls.
