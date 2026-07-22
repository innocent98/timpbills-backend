# Dedicated Virtual Accounts (Paystack DVA) — Design Spec

**Date:** 2026-07-22
**Status:** Approved design, pending implementation plan
**Repos affected:** `timpbills-backend` (primary), `timpbills` (mobile)
**Companion:** `docs/TECHNICAL_PRD.md` (§7.2 Wallet, §8 Integrations, §9 Security)

---

## 1. Summary

Let KYC-verified users provision a permanent dedicated bank account number (Paystack Dedicated Virtual Account / "Dedicated NUBAN") and fund their wallet by ordinary bank transfer to it, as an alternative to the existing Paystack card/checkout funding flow. Paystack notifies us by webhook when money lands; we credit the wallet.

This is an **opt-in** funding rail exposed on the wallet screen, not a replacement for card funding and not part of the KYC flow.

### Locked decisions

| Decision | Choice |
|---|---|
| Placement | Opt-in on the wallet/fund screen (not folded into KYC) |
| BVN storage | **Not stored.** Collect BVN + bank account at setup, pass to Paystack, persist only Paystack's `customer_code` + DVA details |
| Over-cap landed money | Credit the full amount, then **lock outbound spend** until KYC is upgraded. Never reject landed money |
| Fees | Timpbills absorbs the Paystack DVA fee (gross credit: wallet receives the full transferred amount) |
| Compliance path | Required-compliance (General Services): BVN + existing bank account, validated by Paystack |
| Provider | `wema-bank` default, env-configurable; `test-bank` in dev/test |
| Assign method | Single-step `POST /dedicated_account/assign` |
| Tier gate | KYC tier ≥ 1 |

### The core structural fact

The existing Paystack webhook handler is **reference-first**: it requires a transaction reference *we* minted and resolves the user via `Payment.provider_reference`. A DVA bank transfer carries **no such reference** and no `customer` object. It must be resolved by the **receiving account number**. This is the single largest change and the main risk. See §5.

### Two distinct BVN checks (do not conflate)

1. **Dojah KYC (existing):** identity verification. Confirms who the person is (BVN/NIN valid, liveness/face-match on higher tiers). Our gate; upgrades tier.
2. **Paystack customer identification (new):** bank-account-ownership validation. Confirms the supplied bank account belongs to the supplied BVN. Paystack performs this itself when we assign the DVA. Regulatory (CBN/NIBSS) requirement for issuing a virtual account.

They are complementary. Paystack does not replace Dojah; Dojah does not satisfy Paystack.

---

## 2. Data model

New table `virtual_accounts` (one row per user). New Alembic migration in the `202607…` timestamp style; forward-only per PRD §1.7.

```
virtual_accounts
  id                       UUID pk
  user_id                  UUID fk users(id) ON DELETE RESTRICT, UNIQUE   # one DVA per user
  paystack_customer_code   String  not null         # durable identity token; NOT the BVN
  paystack_customer_id     String  null
  dedicated_account_id     String  null             # Paystack account id (requery / deactivate)
  account_number           String  null UNIQUE INDEX # inbound-webhook resolution key
  account_name             String  null
  bank_name                String  null
  bank_slug                String  null             # wema-bank / titan-paystack / test-bank
  currency                 String  not null default 'NGN'
  status                   Enum(VirtualAccountStatus) not null
  failure_reason           String  null             # surfaced to the user on failure
  created_at, updated_at   TimestampMixin
```

`VirtualAccountStatus` (in `app/db/models/_enums.py`):
`pending_identity` → `pending_assign` → `active`; plus `failed`, `deactivated`.

- `pending_identity`: assign called, awaiting `customeridentification.*`.
- `pending_assign`: identity validated, awaiting `dedicatedaccount.assign.*`.
- `active`: account number issued and stored.
- `failed`: identity or assign failed; `failure_reason` holds a user-readable cause.
- `deactivated`: reserved (no v1 UI).

Model registration: add to `app/db/models/__init__.py` (the newer-models registry) so Alembic autogenerate sees it.

### Wallet changes (over-cap lock)

Add to the `wallets` table:
```
  spend_locked         Boolean not null default false
  spend_locked_reason  Enum(SpendLockReason) null      # 'over_cap' (extensible)
```

---

## 3. Paystack integration surface

Extend `app/integrations/paystack/` (`base.py` Protocol, `client.py` real, `fake.py` test double, `factory.py` selection). Signature verification (`signature.py`) is unchanged and reused.

New Protocol methods (verified against live Paystack docs 2026-07-22):

| Method | Endpoint | Required fields |
|---|---|---|
| `create_customer` | `POST /customer` | `email, first_name, last_name, phone` |
| `assign_dedicated_account` | `POST /dedicated_account/assign` | required-compliance: `first_name, middle_name, last_name, phone, preferred_bank, country, account_number, bvn, bank_code`. Returns **202**; result via webhook |
| `fetch_dedicated_account` | `GET /dedicated_account/:id` | account id |
| `requery_dedicated_account` | `GET /dedicated_account/requery` | rate-limited (once / 10 min) |
| `list_dva_providers` | `GET /dedicated_account/available_providers` | — |
| `list_banks` | `GET /bank?country=nigeria` | for the mobile bank picker (reuse if one already exists) |

Reuse the existing tenacity retry pattern. Fake must model the async flow: `assign_dedicated_account` returns a 202-shaped result, and tests then drive the corresponding webhook fixtures.

---

## 4. Provisioning flow

Single-step assign. The BVN lives only inside the request handler and the outbound Paystack call; it is never persisted and is on the log-redaction list (`bvn`).

```
POST /wallet/virtual-account
  auth: standard bearer (NOT a money-move, no X-Pin-Token)
  guards:
    - user.kyc_level.numeric >= 1  else 403 KYC_REQUIRED
    - existing active/pending VirtualAccount for user -> return it (idempotent, no re-provision)
  body: { bvn, account_number, bank_code, preferred_bank? }
  steps:
    - create VirtualAccount(status=pending_identity)
    - split user.full_name -> first_name / middle_name / last_name
    - paystack.create_customer(email, first_name, last_name, phone) -> customer_code
      (idempotent by email on Paystack); persist paystack_customer_code immediately.
      This is REQUIRED before assign: customer_code is NOT NULL and is the resolution
      key for the customeridentification.* / dedicatedaccount.assign.* webhooks (§5.1),
      which arrive with no reference of ours. "Single-step assign" still holds: assign
      reuses the same customer by email.
    - paystack.assign_dedicated_account(...)  -> 202
    - return current status to mobile
  async (webhooks, §5):
    customeridentification.success  -> status = pending_assign
    customeridentification.failed   -> status = failed, failure_reason, notify
    dedicatedaccount.assign.success -> store account_number/bank/name, status = active, notify
    dedicatedaccount.assign.failed  -> status = failed, failure_reason, notify
```

Name split: `full_name` is our only name field. Split on whitespace: first token = `first_name`, last token = `last_name`, middle tokens joined = `middle_name` (empty string if only two tokens). Edge case (single token) sets `last_name = first_name` as a fallback; log a warning.

### KYC hook

DVA is opt-in, so provisioning is **not** auto-triggered on KYC completion. The KYC seam `kyc_service.confirm_verification` is used only for the **unlock** side (§5): on a tier upgrade, clear `spend_locked` if the new cap covers the balance.

---

## 5. Webhook handling

All new branches live in `app/api/v1/endpoints/webhooks.py::paystack_webhook`. Dedupe via the existing `webhook_events.provider_event_id` unique-insert (each Paystack event has a unique `data.id`) — reused unchanged. HMAC verify unchanged.

**Ordering change (critical):** the `dedicated_nuban` charge branch and the identity/assign branches must be evaluated **before** the current `reference` mandatory-check that returns `400 MALFORMED_WEBHOOK`, because DVA events do not carry our reference.

### 5.1 Identity + assign lifecycle

```
customeridentification.success  -> VA by paystack_customer_code -> status = pending_assign
customeridentification.failed   -> status = failed; failure_reason = data.reason
                                   (account resolution failure / name mismatch / BVN mismatch)
                                   notify user "verification_failed"
dedicatedaccount.assign.success -> store data.dedicated_account.{account_number, account_name,
                                   bank.name, bank.slug}, status = active; notify "dva_ready"
dedicatedaccount.assign.failed  -> status = failed; failure_reason; notify
```

### 5.2 Inbound transfer (funding)

```
charge.success AND data.channel == "dedicated_nuban":
  dedupe on data.id
  acct = data.authorization.receiver_bank_account_number
  va   = VirtualAccount where account_number == acct
  if not found -> 200 { status: "unknown_account" } (log; do not error)
  amount = Decimal(data.amount) / 100         # kobo -> naira, GROSS
  tx = TransactionService.create(
         user_id = va.user_id, type = wallet_funding, status = pending,
         reference = generated,
         meta = { funding_channel: "dedicated_nuban",
                  paystack_event_id: data.id,
                  sender_name: data.authorization.sender_name,
                  sender_bank: data.authorization.sender_bank,
                  sender_account_masked: data.authorization.sender_bank_account_number,
                  paystack_fee: <if present> })
  wallet_svc.credit(user_id, amount, over_cap = LOCK)   # §5.3
  TransactionService.transition(tx -> success)
  notify wallet_funded (channel = transfer)
  200 OK
```

No `_claim_payment` step: there is no pre-existing `Payment` row for a DVA inflow (that machinery is checkout-only). The `WebhookEvent` unique insert is the idempotency guard; the synthesized transaction is created only after it succeeds.

### 5.3 Over-cap lock (`wallet_service.credit`)

Add an `over_cap` policy parameter. Default (checkout path) keeps today's behavior: raise `KycCapExceeded`, caller returns 422 and Paystack retries until ops raises the tier. New `LOCK` policy (transfer path only):

```
credit(user_id, amount, over_cap = RAISE | LOCK):
  SELECT wallet FOR UPDATE
  new_balance = balance + amount
  if new_balance > cap:
     if over_cap == RAISE: raise KycCapExceeded          # unchanged, checkout path
     if over_cap == LOCK:
        balance = new_balance                            # credit in full, never reject landed money
        spend_locked = true; spend_locked_reason = over_cap
  else:
        balance = new_balance
  commit
```

**Outbound gate:** every money-out operation (bill purchases in `bill_service`, any wallet debit) checks `wallet.spend_locked` first and returns `423 WALLET_SPEND_LOCKED` with a user-readable message before attempting the debit.

**Unlock:** in `kyc_service.confirm_verification`, after a successful tier upgrade, if `spend_locked and reason == over_cap and balance <= new_cap`, clear the lock. (If still over the new cap, leave locked.)

Rule of thumb: **inbound is never rejected; outbound is gated.**

---

## 6. Fees

Gross credit: the wallet receives the full `data.amount`. Paystack's fee is deducted from settlement, not from the user's credit. Record `paystack_fee` in transaction `meta` for accounting only; it does not reduce the credit. Add config:

```
PAYSTACK_DVA_PREFERRED_BANK = "wema-bank"     # "test-bank" in dev/test
PAYSTACK_DVA_FEE_PERCENT / _CAP_NGN           # accounting/reporting only, not applied to credit
```

(Exact fee figures to be confirmed from the Paystack dashboard; the DVA doc page did not state them.)

---

## 7. Mobile-facing API

Standard `{success, data, error, request_id}` envelope.

| Method | Path | Purpose |
|---|---|---|
| POST | `/wallet/virtual-account` | provision (body: `bvn`, `account_number`, `bank_code`, `preferred_bank?`) |
| GET | `/wallet/virtual-account` | current DVA: `{ status, account_number, account_name, bank_name, failure_reason }` |
| GET | `/wallet/banks` | bank list for the account picker (proxy Paystack `GET /bank`; reuse if present) |

New DI wiring in `app/api/deps.py` for `VirtualAccountService`.

---

## 8. Mobile (Flutter)

Design-system components only (`AppText`, `AppButton`, `BrandTextField`, `context.semantic`). **No em-dashes or en-dashes in any user-facing copy.**

- **Wallet screen card:** no DVA -> "Set up your account number." Active -> show account number + bank + copy button + "Transfer to this account to top up instantly."
- **Setup screen:** bank picker + account number + BVN; name/phone prefilled read-only. Copy: "We use this to confirm a bank account in your name. Your BVN is sent securely to our bank partner and is not stored."
- **Pending screen:** "Setting up your account. This usually takes a few moments." Polls `GET /wallet/virtual-account` on the existing `funding_status_page` backoff pattern.
- **Ready:** show the account. **Failed:** show `failure_reason` in plain language + Retry.
- Add `lib/features/wallet/` DVA controller + repo (http + fake), following the airtime feature shape.

State machine mirrors the backend: `none → pending → active | failed`.

---

## 9. Notifications

New events via the existing `NotificationService` (push + in-app; SMS remains reserved for KYC-1 per PRD §5b deviation):
- `dva_ready` — account provisioned.
- `dva_failed` — provisioning failed (include reason).
- `wallet_funded` with `channel = transfer` — reuse existing context builder (already accepts `channel`).

---

## 10. Testing

Backend (pytest, fakes not live providers):
- Provisioning idempotency: two calls -> one Paystack customer, one row.
- Name-split edge cases.
- Webhook resolution by `receiver_bank_account_number` -> correct user; unknown account -> 200 no-op.
- Over-cap landed money: credits in full + sets `spend_locked`, never 422.
- Spend-lock blocks bill purchase (`423 WALLET_SPEND_LOCKED`).
- KYC upgrade clears the lock when the new cap covers the balance; leaves it if still over.
- Async assign state machine: `pending_identity → pending_assign → active`, and both failure branches.
- Dedupe on replayed `data.id`.
- Extend `fake.py` with the new methods + webhook fixtures.

Mobile (mocktail / fake repo via `ProviderScope.overrides`): controller state machine, setup validation, pending-poll, failed + retry.

---

## 11. Reuse vs build

**Reuse:** `wallet_service.credit` (extended with the policy param), `webhook_event` dedupe, `signature.py`, `paystack/factory.py` + `fake.py` patterns, `transaction_service.create/transition`, `notification_service` (channel-aware), `funding_status_page` poll pattern, the airtime feature shape.

**Build new:** `app/db/models/virtual_account.py` + migration; wallet columns + migration; new Paystack methods; `app/services/virtual_account_service.py`; new webhook branches; new wallet endpoints + DI; DVA config; `lib/features/wallet/` DVA screens/controller/repo.

---

## 12. Out of scope (YAGNI, v1)

Multiple DVAs per user; DVA deactivation UI; auto-refund of over-cap money (we lock instead); storing BVN; multi-step assign flow; SMS for DVA events.

---

## 13. Open items to confirm before build

1. Confirm Timpbills' Paystack **compliance category** and that **DVA is enabled** on the account (95% confidence it is General Services / required-compliance; a dashboard/support confirmation).
2. Confirm the **DVA fee** figures from the Paystack dashboard.
3. Confirm **provider availability** (`wema-bank` vs `titan-paystack`) on the live account.
