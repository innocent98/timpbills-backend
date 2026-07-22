# SDD Progress — Dedicated Virtual Accounts (Paystack DVA) · Backend

Plan: docs/superpowers/plans/2026-07-22-dedicated-virtual-accounts-backend.md
Spec: docs/superpowers/specs/2026-07-22-dedicated-virtual-accounts-design.md
Branch: develop
Base HEAD before Task 1: 8edbc38

Build against fakes (FORCE_FAKE_PROVIDERS). Live Paystack cutover gated on §13 confirmations.
Uncommitted-and-must-not-commit: tests/api/_kyc_contract/*.json (real Dojah widget IDs, unrelated).

- [x] Task 1: complete (19ec762, review clean). Fixed brief's enum DuplicateObject bug via repo precedent (postgresql.ENUM create_type True/False split).
- [x] Task 2: complete (1614da8, code approved). CAVEAT: haiku implementer mis-reported suite as '347 passed'; reviewer verified TRUE full suite = 923 passed / 3 pre-existing unrelated failures / 927 collected, NO regression. Do not trust that implementer's self-reported counts. Minor (triage): test_config_dva asserts fee values by type not value (inherited from brief).
- [x] Task 3: complete (62b9cb3, review clean). All 3 named checks pass (retry parity, deterministic fake create_customer, dedicated_nuban fixture complete for Tasks 9/10). True suite 927 passed/3 pre-existing. Minors (triage): fake.create_customer uses per-process hash() not sha256 (fine within-process; Task 6 idempotency tests run in one process); client falsy-zero id->None collapse (inherited from brief).
- [x] Task 4: complete (f628acb, review clean). All 4 named money-path checks pass. RAISE byte-identical, LOCK credits full+locks in one txn, clear only when balance<=cap. Minors (triage): clear_spend_lock doesn't refresh w.balance_cap field (decision uses fresh cap, not a bug); raise_if_spend_locked has no FOR UPDATE (debit re-locks, matches spec).
- [x] Task 5: complete (22176a9, review clean). All 3 structural checks pass (gate before debit at _execute_bill funnel, all 4 endpoints -> 423 WALLET_SPEND_LOCKED, card flow routes same path). Minors (FINAL-REVIEW FIX WAVE): (a) bill_service.py comment says 'tx stays pending' but code transitions to failed - reword; (b) unused `import json` in tests/api/test_bills_spend_lock.py; (c) add co-located 'unlocked -> 200' positive assertion.
- [x] Task 6: complete (6d9689b + fix 326f50b, review clean after fix). All 5 named checks pass (customer-code-before-assign, idempotent no-double-provision, BVN never persisted/logged, tier>=1 guard, name-split edges). Important fix: assign-throws now sets row failed (recoverable) instead of wedging at pending_identity; +will_raise_on_assign fake hook. 938 passed/3 pre-existing.
- [x] Task 7: complete (f2fd9e3, review clean). All 5 named checks pass (404 NO_VIRTUAL_ACCOUNT, banks={banks:[{name,code,slug}]}, KycRequired->403, no BVN logging, DI mirrors siblings). Banks-shape deviation from brief sample = correct contract call. Minors (triage): VirtualAccountResponse construction duplicated in POST+GET (extract _to_va_response); task-7-brief banks snippet now stale.
- [x] Task 8: complete (1a2bbb8, review clean). Both events follow existing pattern, push+in-app only (NO SMS path), no dashes, dva_failed carries reason. Minor (FINAL-REVIEW FIX WAVE): _push_copy uses ctx.get('bank_name','your bank') but build_dva_context always returns bank_name='' -> empty bank renders 'Transfer to X () to...'; change to ctx.get('bank_name') or 'your bank'.
- [x] Task 9: complete (pending commit). Reorder verified: event_id-only guard -> dedupe insert -> DVA lifecycle branch (resolved by customer_code, both flat data.customer_code and nested data.customer.customer_code shapes) -> reference-mandatory check -> existing charge/transfer flow. 4 new tests green; all 7 pre-existing tests/api/test_webhooks_paystack.py tests still pass unmodified. True full suite: 949 passed / 3 pre-existing (Dojah/admin-config, real .env creds) / 1 xfailed.
- [ ] Task 10: webhook funding branch
- [ ] Task 11: KYC unlock hook

## Minor findings (for final review triage)

- Task 1 minors (triage at final): redundant non-unique ix on virtual_accounts.user_id (already UNIQUE, inherited from brief Step 8 — avoid repeating pattern); wallet.spend_locked server_default "false" string vs sa.false() in migration (cosmetic).
- Pre-existing (NOT our regression): 3 failures from real Dojah creds in local .env (test_admin_config_defaults + kyc contract). Confirmed via git-stash. Do not attribute to DVA.


## MOBILE CONTRACT (align backend Task 7 to these):
- GET /wallet/banks MUST return data = { banks: [ {name, code, slug} ] } (mobile depends on this shape).
- GET /wallet/virtual-account with no account MUST return 404 code NO_VIRTUAL_ACCOUNT (mobile maps 404 -> null).
