# Timpbills Backend - Master Build Checklist

Single source of truth for what's built, in progress, and ahead. Kept as
GitHub task lists so progress is visible at a glance. Complements the SOPs
(`docs/sop/`, post-ship record of one change) and FE integration guides
(`docs/fe-integration-guide-*.md`).

> This checklist was created on 2026-09-12 alongside the admin edit-user
> feature. It currently maps the Admin console API surface (the area of active
> work); other modules will be back-filled as they are touched.

## Snapshot

- Modules mapped: 1 (Admin console API)
- This file is NOT yet a complete map of the whole backend - it is seeded from
  the admin surface and grows as areas are worked. Treat unlisted modules as
  "not yet mapped here", not "not built".

## Admin console API (`/api/v1/admin`) - in progress

Auth: opaque admin session cookie (`require_admin`) + double-submit CSRF
(`require_admin_csrf`) on writes.

- [x] Manual refund trigger - `POST /admin/refunds/{reference}/trigger`
- [x] Resend verification email - `POST /admin/users/{user_id}/resend-verification-email`
      (SOP: `2026-08-29-admin-resend-verification-email.md`)
- [x] Refunds list - `GET /admin/refunds`
- [x] Overview metrics - `GET /admin/overview`
- [x] Transactions list - `GET /admin/transactions`
- [x] Transaction detail - `GET /admin/transactions/{reference}`
- [x] Transaction requery - `POST /admin/transactions/{reference}/requery`
- [x] Users list - `GET /admin/users`
- [x] User detail - `GET /admin/users/{user_id}`
- [x] Notifications list - `GET /admin/notifications`
- [x] **Edit basic user info - `PATCH /admin/users/{user_id}`** (2026-09-12)
  - [x] `AdminUserUpdateRequest` schema (`extra="forbid"`, all optional, PATCH semantics)
  - [x] `AdminService.update_user` (name/email/phone + verified-flag + token-revoke side effects)
  - [x] Error mapping: NO_FIELDS 400 / USER_NOT_FOUND 404 / *_ALREADY_IN_USE 409 / INVALID_PHONE 422
  - [x] PII-safe audit log-line (changed field NAMES only)
  - [x] Tests: `tests/api/test_admin_update_user.py` (10 cases, green)
  - [x] SOP: `2026-09-12-admin-edit-user-basic-info.md`
  - [x] FE guide: `fe-integration-guide-admin-edit-user.md` (captured-live)

## Backlog / upcoming (admin console)

- [ ] Admin action audit trail (a non-tx audit surface; name/email/phone edits
      currently log-only, no persisted row).

## Deferred follow-ups

- [ ] Pre-existing local test drift: `tests/core/test_admin_config_defaults.py`
      and `tests/core/test_config_dojah.py` fail locally on `.env` config
      defaults (unrelated to feature work; confirmed red before the edit-user change).
