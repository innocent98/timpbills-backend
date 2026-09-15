# Absorb Paystack card funding fee

## What shipped
Wallet card funding now charges the user EXACTLY the amount they enter.
Timpbills absorbs the Paystack card fee instead of adding it on top of the
charge. A server-side minimum funding amount (N100) was added at the same
time.

- **Endpoint:** `POST /api/v1/wallet/fund` (`app/api/v1/endpoints/wallet.py`).
- **Webhook:** card `charge.success` handler in
  `app/api/v1/endpoints/webhooks.py` now records Paystack's real fee for
  reporting.
- Commit(s): see `git log` on `develop` for `feat(wallet): absorb Paystack
  card funding fee` (local only, not pushed).

## Why
Previously `fund_wallet` computed the Paystack local-card fee via
`_calculate_fee(amount)` and charged the user `amount + fee`
(`gross_kobo = int((body.amount + fee) * 100)`), while the webhook credited
only `tx.amount`. The customer therefore paid the processor fee on every
top-up, which is hostile UX for a wallet (you put in N5,000 and get charged
N5,175). DVA (bank-transfer) funding already absorbed its fee
(`webhooks.py`, dedicated_nuban path credits GROSS), so card funding was the
odd one out. Funding is now treated as a cost center recovered via
bill-payment margins, consistent across both funding channels.

## How
- **Charge exact amount.** `gross_kobo = int(body.amount * 100)` (removed the
  `+ fee`). The transaction row is created with `fee = Decimal("0.00")` and
  `FundWalletResponse.fee` is `0.00`.
- **Removed** the now-unused `_calculate_fee` helper.
- **Minimum funding gate.** New setting `WALLET_MIN_FUND_NAIRA = 100`
  (`app/core/config.py`). `fund_wallet` raises `422 AMOUNT_TOO_LOW` when
  `body.amount < settings.WALLET_MIN_FUND_NAIRA`. The check runs BEFORE
  `idem.lookup_or_acquire`, so a pure validation failure never consumes an
  idempotency slot and never calls Paystack. The message is built from the
  setting (no hardcoded 100) and uses the real naira sign:
  `f"Minimum funding amount is ₦{settings.WALLET_MIN_FUND_NAIRA:,}."`.
- **Webhook fee recording.** The card `charge.success` success transition
  context changed from `{"paystack_event_id": event_id}` to
  `{"paystack_event_id": event_id, "paystack_fee": data.get("fees")}`,
  mirroring the DVA path. This captures Paystack's actual fee (kobo) for
  finance reporting WITHOUT charging the user. What is credited is unchanged
  (`tx.amount`).
- **Config fields retained-but-unused.** The four `PAYSTACK_CARD_FEE_*`
  fields were referenced only by `_calculate_fee`. They are LEFT in
  `config.py` (with a comment marking them retained-but-unused since
  2026-09-15) to avoid any env-mismatch risk in deployed `.env` files.
  `Settings.Config` uses `extra = "ignore"`, so their presence is harmless
  either way; verified in `app/core/config.py`.

### Key decision
Absorb rather than surface the fee. The alternative (keep passing the fee but
show it more clearly in the app) was rejected: it keeps the wallet balance
mismatch between "what I paid" and "what landed", and DVA funding already set
the absorb precedent. There is no wallet-to-bank withdrawal path anywhere, so
absorbed funding cost is not compounded by an outbound-fee leak.

## What's involved
| Area | Path |
|---|---|
| Fund endpoint | `app/api/v1/endpoints/wallet.py` (`fund_wallet`; `_calculate_fee` removed) |
| Webhook fee capture | `app/api/v1/endpoints/webhooks.py` (card `charge.success` transition context) |
| Config | `app/core/config.py` (`WALLET_MIN_FUND_NAIRA`; `PAYSTACK_CARD_FEE_*` retained-but-unused) |
| Tests | `tests/api/test_wallet_fund.py`, `tests/api/test_webhooks_paystack.py` |
| FE guide | `docs/fe-integration-guide-wallet-funding.md` |
| Captures | `docs/captures/wallet-funding/*.json` |

No DB migration (no schema change; fee column already existed and is written
as 0.00).

## Verification
- **Tests (TDD).** Wrote three tests first, confirmed red against the old
  code, implemented, confirmed green:
  - `test_fund_wallet_charges_exact_amount_and_absorbs_fee` - asserts the
    Paystack fake `initialize` was called with `amount_kobo == int(amount *
    100)` (no fee), response `fee == "0.00"`, and the persisted tx `fee ==
    Decimal("0.00")`.
  - `test_fund_wallet_below_minimum_returns_422_without_touching_paystack` -
    below-minimum returns `422 AMOUNT_TOO_LOW`, Paystack was NOT called, and
    no idempotency slot leaked (same key + valid amount then succeeds).
  - `test_charge_success_records_paystack_fee_in_transition_context` - the
    success `TransactionEvent.context` carries `paystack_fee` (data.fees).
  - Commands:
    - `poetry run ruff check app tests` -> All checks passed.
    - `poetry run pytest tests/api/test_wallet_fund.py tests/api/test_webhooks_paystack.py tests/e2e/test_funding_flow.py --no-cov -q` -> 19 passed.
    - `poetry run pytest --no-cov -q` -> 1035 passed, 1 skipped, 1 xfailed, 3 failed.
- **Pre-existing failures (NOT caused by this change):**
  `tests/core/test_admin_config_defaults.py::test_admin_cookie_defaults`,
  `tests/core/test_config_dojah.py::{test_dojah_defaults,test_dojah_optional_secrets_default_none}`.
  Confirmed by stashing `config.py` and re-running: they fail identically
  without this change. They are local `.env`-override drift (already tracked
  in the master checklist Deferred follow-ups); CI's clean env passes them.
- **CI format step** (`ruff format --check`, `continue-on-error: true`) flags
  pre-existing repo-wide formatting drift in untouched lines; the lines added
  by this change are format-clean. Not reformatting untouched code (no
  drive-by).

## Operate / roll back
- No runtime config required. `WALLET_MIN_FUND_NAIRA` defaults to 100; override
  via env if the minimum changes.
- Roll back by reverting the commit(s). No migration to reverse.

## Follow-ups
- Finance/reporting can now read `transaction_events.context.paystack_fee`
  (kobo) for card fundings to total absorbed card cost; a reporting query or
  admin surface for this is not yet built.
- `PAYSTACK_CARD_FEE_*` config fields are retained-but-unused; remove in a
  later cleanup once no deployed `.env` references them.
