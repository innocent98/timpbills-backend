# FE Integration Guide: Admin - Edit basic user info

Endpoint the platform-admin console calls to edit a user's basic identity
fields (full name, email, phone) from the user-profile page.

> Every request/response body, status code, and error string below was
> CAPTURED LIVE against the running app via the API test client
> (`tests/api/` harness) on 2026-09-12, not written from the schema. IDs and
> referral codes in the examples are from throwaway test rows and are
> obviously-fake placeholders. See the verification table at the end.

## Endpoint

`PATCH /api/v1/admin/users/{user_id}`

- **Auth:** admin session cookie (`admin_session`) + double-submit CSRF. Send
  the CSRF value (from the `admin_csrf` cookie) in the `X-CSRF-Token` header on
  every call. Same as the refund / resend-verification actions -> reuse the
  console's existing admin `call()` wrapper (`credentials: "include"` +
  `X-CSRF-Token`).
- **Body:** JSON, all fields optional, `extra` keys forbidden. Send ONLY the
  fields you are changing (true PATCH).

| Field | Type | Rules |
|---|---|---|
| `full_name` | string | 2..80 chars; server trims surrounding whitespace |
| `email` | string (email) | valid email shape (see EmailStr trap below); normalised to lowercase server-side |
| `phone` | string | Nigerian number in local (`080...`), `234...`, or `+234...` form; normalised to E.164 server-side |

## Success responses

The success body is the SAME shape as `GET /api/v1/admin/users/{id}` (a full
user-detail payload), so on 200 you can replace your user-detail state
directly with `response.data`.

### 1. Name change -> 200

Request: `{"full_name": "Ada B. Lovelace"}` (sent as `"  Ada B. Lovelace  "` with
surrounding spaces; server trims).

```json
HTTP 200
{
  "success": true,
  "data": {
    "id": "53180ca1-9961-444e-92a2-3480cbe98841",
    "full_name": "Ada B. Lovelace",
    "email": "ada@example.test",
    "phone": "+2348030000001",
    "kyc_tier": 0,
    "email_verified": true,
    "phone_verified": true,
    "status": "active",
    "created_at": "2026-09-12T17:47:24.536380",
    "wallet_balance": "0.00",
    "wallet_cap": "0.00",
    "referral": { "code": "ZV2VWJ", "referred_count": 0 },
    "recent_transactions": []
  },
  "error": null,
  "request_id": "5b928381d0844514abefb935aa5a426b"
}
```

### 2. Email change -> 200, `email_verified` flips to false

Request: `{"email": "grace.hopper@example.com"}`. Note `email` comes back
lowercased and `email_verified` is now `false`.

```json
HTTP 200
{
  "success": true,
  "data": {
    "id": "4a8c16a6-1c98-4a2f-bb49-6f5df66e53f9",
    "full_name": "Ada Lovelace",
    "email": "grace.hopper@example.com",
    "phone": "+2348030000002",
    "kyc_tier": 0,
    "email_verified": false,
    "phone_verified": true,
    "status": "active",
    "created_at": "2026-09-12T17:48:40.518516",
    "wallet_balance": "0.00",
    "wallet_cap": "0.00",
    "referral": { "code": "CRQCVZ", "referred_count": 0 },
    "recent_transactions": []
  },
  "error": null,
  "request_id": "521b4ab3cc1d49f389b4d8d6306772d8"
}
```

### 3. Phone change -> 200, `phone_verified` flips to false (user is signed out)

Request: `{"phone": "08039999001"}`. `phone` comes back in E.164 and
`phone_verified` is now `false`.

```json
HTTP 200
{
  "success": true,
  "data": {
    "id": "9dd1419f-d89d-4070-aa80-e8ba22bf7d47",
    "full_name": "Ada Lovelace",
    "email": "katherine@example.test",
    "phone": "+2348039999001",
    "kyc_tier": 0,
    "email_verified": true,
    "phone_verified": false,
    "status": "active",
    "created_at": "2026-09-12T17:47:24.624143",
    "wallet_balance": "0.00",
    "wallet_cap": "0.00",
    "referral": { "code": "QHXQHF", "referred_count": 0 },
    "recent_transactions": []
  },
  "error": null,
  "request_id": "e24ab607c8864339941e9b161608757c"
}
```

## UX consequence the FE MUST surface

**A phone change signs the user out of every device.** Server-side, changing a
user's phone revokes all of that user's access + refresh tokens (their next app
request returns 401 and they must log in again). This is intentional (a phone is
a login identifier), but it is invisible in the 200 response. When an operator
edits the phone, the console MUST warn: e.g. "Changing the phone number will
sign this user out of all their devices." An email change does NOT sign them out
(it only clears `email_verified`).

## Error responses

Standard envelope: `{"success": false, "data": null, "error": {"code","message","details"}, "request_id"}`.

| HTTP | `error.code` | When |
|---|---|---|
| 400 | `NO_FIELDS` | Body had none of `full_name`/`email`/`phone` (e.g. `{}`) |
| 404 | `USER_NOT_FOUND` | Unknown or non-UUID `user_id` |
| 409 | `EMAIL_ALREADY_IN_USE` | New email belongs to another account |
| 409 | `PHONE_ALREADY_IN_USE` | New phone belongs to another account |
| 422 | `INVALID_PHONE` | Phone not a valid Nigerian number |
| 422 | `VALIDATION_ERROR` | Pydantic rejected the body (bad email shape, extra key, name < 2 chars) |
| 403 | `CSRF_FAILED` | Missing/mismatched `X-CSRF-Token` vs `admin_csrf` cookie |
| 401 | `ADMIN_AUTH_REQUIRED` | No/invalid admin session |

### 400 NO_FIELDS (empty body)
```json
HTTP 400
{
  "success": false,
  "data": null,
  "error": { "code": "NO_FIELDS", "message": "No fields to update.", "details": null },
  "request_id": "1f880244efde4307b54ed1b60545faf5"
}
```

### 404 USER_NOT_FOUND
```json
HTTP 404
{
  "success": false,
  "data": null,
  "error": { "code": "USER_NOT_FOUND", "message": "User not found", "details": null },
  "request_id": "052060cca4de48ee8775ba5f65e8602f"
}
```

### 409 EMAIL_ALREADY_IN_USE
```json
HTTP 409
{
  "success": false,
  "data": null,
  "error": { "code": "EMAIL_ALREADY_IN_USE", "message": "That email is already in use by another account.", "details": null },
  "request_id": "b5c92f018ea842f6bc273f27f3808d32"
}
```

### 409 PHONE_ALREADY_IN_USE
```json
HTTP 409
{
  "success": false,
  "data": null,
  "error": { "code": "PHONE_ALREADY_IN_USE", "message": "That phone number is already in use by another account.", "details": null },
  "request_id": "ad0b0386d14a463cbea6ac31b53f8a73"
}
```

### 422 INVALID_PHONE (request `{"phone": "12345"}`)
```json
HTTP 422
{
  "success": false,
  "data": null,
  "error": { "code": "INVALID_PHONE", "message": "Enter a valid Nigerian phone number.", "details": null },
  "request_id": "bafe2bc63c4141559e5cd75882621aaa"
}
```

### 422 VALIDATION_ERROR - extra key (request `{"kyc_tier": 2}`)
Only `full_name`/`email`/`phone` are editable here; any other key is rejected.
Note `details` is a LIST here (Pydantic error array), unlike the coded errors
above where `details` is `null`.
```json
HTTP 422
{
  "success": false,
  "data": null,
  "error": {
    "code": "VALIDATION_ERROR",
    "message": "Validation failed",
    "details": [
      { "type": "extra_forbidden", "loc": ["body", "kyc_tier"], "msg": "Extra inputs are not permitted", "input": 2 }
    ]
  },
  "request_id": "aed8aa8852cb44d9b8c37a4405d101fc"
}
```

### 422 VALIDATION_ERROR - name too short (request `{"full_name": "A"}`)
```json
HTTP 422
{
  "success": false,
  "data": null,
  "error": {
    "code": "VALIDATION_ERROR",
    "message": "Validation failed",
    "details": [
      { "type": "string_too_short", "loc": ["body", "full_name"], "msg": "String should have at least 2 characters", "input": "A", "ctx": { "min_length": 2 } }
    ]
  },
  "request_id": "1c5311d3e0a540bbb1ef169bdc261749"
}
```

### 403 CSRF_FAILED (no `X-CSRF-Token` header)
```json
HTTP 403
{
  "success": false,
  "data": null,
  "error": { "code": "CSRF_FAILED", "message": "CSRF check failed", "details": null },
  "request_id": "032a8ca39eff4fc8adc23a770ee9493d"
}
```

### 401 ADMIN_AUTH_REQUIRED (no admin session)
```json
HTTP 401
{
  "success": false,
  "data": null,
  "error": { "code": "ADMIN_AUTH_REQUIRED", "message": "Admin auth required", "details": null },
  "request_id": "3f8e53e735a248c89e4e8e1494f51630"
}
```

## Field-nesting / validation traps

- **`details` shape differs by error family.** For the coded business errors
  (`NO_FIELDS`, `USER_NOT_FOUND`, `*_ALREADY_IN_USE`, `INVALID_PHONE`,
  `CSRF_FAILED`, `ADMIN_AUTH_REQUIRED`), `error.details` is `null`. For
  `VALIDATION_ERROR`, `error.details` is a LIST of Pydantic issue objects. Do
  not assume a single shape.
- **`INVALID_PHONE` (422) vs `VALIDATION_ERROR` (422)** are BOTH 422 but have
  different `code`s. A malformed-but-string phone (`"12345"`) is caught in the
  service as `INVALID_PHONE`. Branch on `error.code`, not on the status alone.
- **EmailStr rejects special-use TLDs.** An email like `x@example.test` is
  rejected by the schema with 422 `VALIDATION_ERROR`
  (`"value is not a valid email address: The part after the @-sign is a
  special-use or reserved name..."`) BEFORE the service runs. Use real
  deliverable-looking domains; `.test`/`.localhost`/`.invalid`/`.example` fail.
- **`phone_verified` naming.** The response field is `phone_verified` (the ORM
  column is `is_phone_verified`); the API renames at the boundary. Read
  `phone_verified` on this payload.
- **Verification flags after edit.** `email_verified` and `phone_verified` are
  the freshly-recomputed values; an email edit sets `email_verified=false`, a
  phone edit sets `phone_verified=false`. The other flag is unchanged.

## Verification table

| Behaviour | Verified live? |
|---|---|
| 200 name change returns full user-detail payload | Yes |
| 200 email change -> `email_verified: false`, email lowercased | Yes |
| 200 phone change -> `phone_verified: false`, phone E.164 | Yes |
| Phone change revokes user tokens (signs user out) | Yes (asserted in `test_admin_update_user.py`: `tokens_revoked_at` set + refresh key cleared) |
| 400 NO_FIELDS on empty body | Yes |
| 404 USER_NOT_FOUND | Yes |
| 409 EMAIL_ALREADY_IN_USE | Yes |
| 409 PHONE_ALREADY_IN_USE | Yes |
| 422 INVALID_PHONE | Yes |
| 422 VALIDATION_ERROR (extra key) | Yes |
| 422 VALIDATION_ERROR (name too short) | Yes |
| 403 CSRF_FAILED | Yes |
| 401 ADMIN_AUTH_REQUIRED | Yes |
| EmailStr rejects `.test` TLD | Yes (observed during capture) |
