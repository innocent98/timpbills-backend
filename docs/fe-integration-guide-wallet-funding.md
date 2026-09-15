# FE Integration Guide: Wallet funding (card)

How the app tops up a user's wallet via a Paystack card checkout, and what
changed with card-fee absorption (2026-09-15).

> Every request/response body, status code, and error string below was
> CAPTURED LIVE against the running app via the API test client
> (`tests/api/` harness) on 2026-09-15, not written from the schema. The
> raw captures are committed under `docs/captures/wallet-funding/`. IDs,
> references, and request IDs in the examples are from throwaway test rows
> and are obviously-fake placeholders. See the verification table at the end.

## The headline change

Timpbills now ABSORBS the Paystack card fee. The user is charged EXACTLY the
amount they enter, and their wallet is credited that same amount in full.

- The `fee` field in the fund response is now always `"0.00"`.
- Do NOT add a fee line to the top-up confirmation UI. The amount the user
  types is the amount charged and the amount received. No "+ processor fee".
- This matches bank-transfer (DVA) funding, which already credited the full
  transferred amount.

## Endpoint

`POST /api/v1/wallet/fund`

- **Auth:** bearer access token (`Authorization: Bearer <token>`).
- **Money operation headers (required):**
  - `X-Pin-Token` - a valid PIN token (from `POST /auth/pin/verify`). Reuse
    the cached token within its window; do not re-prompt per transaction.
  - `Idempotency-Key` - a client-generated UUID. Reuse the SAME key on retry
    of the same top-up so a network retry never creates a second checkout.
- **Body:**

| Field | Type | Rules |
|---|---|---|
| `amount` | decimal string | `> 0`, and `>= 100` (server minimum). Naira. |

### Success (200)

Request:

```json
{ "amount": "5000.00" }
```

Response (`200`):

```json
{
  "success": true,
  "data": {
    "reference": "202609151208TMP361f2cY7TNJ5ZR30",
    "authorization_url": "https://checkout.paystack.com/fake/202609151208TMP361f2cY7TNJ5ZR30",
    "amount": "5000.00",
    "fee": "0.00"
  },
  "error": null,
  "request_id": "4e0c5c9c6b384164acb030d087dc3874"
}
```

- `authorization_url` is the Paystack checkout URL to open in the in-app
  WebView. (Above it is the fake test URL; in prod it is a real
  `checkout.paystack.com` link.)
- `reference` is the transaction reference the FE polls / matches on.
- `amount` == what was entered. `fee` == `"0.00"` (absorbed).

## Errors the FE must handle

All errors use the standard envelope:
`{ "success": false, "data": null, "error": { "code", "message", "details" }, "request_id" }`.

### Below the minimum: 422 `AMOUNT_TOO_LOW`

Request:

```json
{ "amount": "50.00" }
```

Response (`422`):

```json
{
  "success": false,
  "data": null,
  "error": {
    "code": "AMOUNT_TOO_LOW",
    "message": "Minimum funding amount is ₦100.",
    "details": null
  },
  "request_id": "2d6af23ab8524c1ca092fd1f3e8dd4fa"
}
```

- `₦` is the naira sign (this is how JSON escapes it; the string is
  "Minimum funding amount is (naira sign)100."). Surface the message as-is.
- This is a pure validation failure: it does NOT consume the idempotency key
  and does NOT create a Paystack checkout. The FE may reuse the same
  `Idempotency-Key` on the corrected (valid) retry.

### Other error codes on this endpoint (shapes unchanged by this work)

| Status | `code` | When |
|---|---|---|
| 401 | `PIN_TOKEN_REQUIRED` | `X-Pin-Token` missing/invalid. |
| 400 | `IDEMPOTENCY_KEY_REQUIRED` | `Idempotency-Key` header missing. |
| 409 | `IDEMPOTENCY_CONFLICT` | Same key reused with a DIFFERENT body. |
| 409 | `TX_IN_FLIGHT` | Same request still processing; retry shortly. |
| 422 | `KYC_LIMIT_EXCEEDED` | Amount would push balance past the tier cap. `details` carries `remaining_headroom` and `balance_cap`. |

## Field-nesting traps

- `fee` lives at `data.fee` on the fund response and is a decimal STRING
  (`"0.00"`), not a number. It is now always `"0.00"`; do not compute or
  display a fee from it.
- `amount` is a decimal string too (`"5000.00"`), not a number.
- Error details are under `error.details` and may be `null` (as on
  `AMOUNT_TOO_LOW`) or an object (as on `KYC_LIMIT_EXCEEDED`). Guard for
  `null`.

## UX consequences to surface

- Show the entered amount as the exact charge. No fee line, no "you will be
  charged N X + fee". If old app builds still render a fee row, it will now
  read N0.00.
- Enforce the N100 minimum client-side too for a fast inline error, but treat
  the server `AMOUNT_TOO_LOW` as the source of truth.

## Verification table

| Behaviour | How verified |
|---|---|
| Fund success returns `fee: "0.00"` and charged amount | Verified live (test client) - `docs/captures/wallet-funding/fund_success.json` |
| `AMOUNT_TOO_LOW` 422 body + message | Verified live (test client) - `docs/captures/wallet-funding/fund_amount_too_low.json` |
| Paystack charged exactly `amount` (no fee added) | Verified via test - `test_fund_wallet_charges_exact_amount_and_absorbs_fee` asserts the Paystack fake was called with `amount_kobo == int(amount*100)`. Cannot be exercised against real Paystack from the harness. |
| Webhook records `paystack_fee` for reporting | Verified via test (NOT live) - `test_charge_success_records_paystack_fee_in_transition_context`. This is a Paystack->server webhook and cannot be driven against real Paystack from the harness. |
| Other error codes (`PIN_TOKEN_REQUIRED`, `IDEMPOTENCY_*`, `KYC_LIMIT_EXCEEDED`) | Pre-existing, verified in `tests/api/test_wallet_fund.py`; unchanged by this work. |
