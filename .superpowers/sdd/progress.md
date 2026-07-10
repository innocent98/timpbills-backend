# SDD Progress — KYC BVN/NIN Dojah (Track A · Backend)

Plan: docs/superpowers/plans/2026-07-10-kyc-bvn-nin-dojah.md
Branch: develop
Base HEAD before Track A: f20ddfb

- [x] A1: Dojah config settings — complete (3a3deb6, review clean)
- [x] A2: schema + protocol + FakeKycProvider — complete (1b8e347, async fix 31d99e1, review clean). Fake field semantics blessed. NOTE for A6: identity cross-check must fail only on positive mismatch (fake returns identity None).
- [x] A3: DojahClient + webhook signature + factory — complete (387bcf4, review clean). Live Dojah endpoint/schema isolated in _ENDPOINT_PATH/_parse_result (open item).
- [x] A4: KycRecord model + migration — complete (9a04aad, masked_id VARCHAR(16) fix 9c4d266, review clean). CASCADE FK (matches otp/push_token). Migration 202607101300.
- [x] A5: Wallet cap update — complete (2f24a6b, review clean). tier_3=None (true skip-check), _UNLIMITED_CAP=900B column filler only. tier_1=300k.
- [x] A6: KycService start + confirm — complete (3ddf8cd, review clean). Row-lock idempotency, validation matrix, tier_after keyed by type. Fake now defaults unknown refs → success. confirm_verification(reference_id, source) is async.
- [x] A7: KYC endpoints + schemas — complete (e36b448, UPPER_SNAKE+webhook-limit fix e7a328a, review clean). config/start/confirm/status/webhook. Minor (triage): kyc.py mypy return-type gaps (repo-consistent).
- [x] A8: /auth/me tier_3 regression — complete (698caa8, passes immediately, guard confirmed). TRACK A COMPLETE.

## FINAL REVIEW (backend): 2 Important + minors. Fixed in 28a2977 (monotonic tier apply, lock-before-network, 5xx-only retry) — verified correct, 55 tests green. Accepted minor: webhook 60/min limit (Dojah retries + /status backstop).

## Pre-existing (not our regression)
- tests/core/test_admin_config_defaults.py::test_admin_cookie_defaults fails on develop HEAD (local .env leak?) — verify in final wave, do not attribute to KYC.

## Minor findings (for final review triage)
