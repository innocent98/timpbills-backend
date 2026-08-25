# DVA provisioning hangs forever — webhook 400 + no self-healing

## What shipped
Fix for a production bug where creating a Dedicated Virtual Account (DVA) left
the UI spinning "setting up your account number" indefinitely. Three parts:

1. **Webhook accepts id-less DVA events** — `app/api/v1/endpoints/webhooks.py`.
2. **Fake made faithful to real Paystack** — `app/integrations/paystack/fake.py`
   (the existing DVA endpoint tests now exercise the real payload shape).
3. **Self-healing recovery sweep** — `reconcile_pending_dva_assign`
   (`app/workers/tasks/reconcile_tasks.py`, every 2 min) that requeries Paystack
   by `customer_code` and backfills stuck accounts. Recovers already-stranded
   accounts and any future missed/rejected assign webhook.

## Why (root cause)
`POST /wallet/virtual-account` succeeded (200), then Paystack's DVA lifecycle
webhooks (`customeridentification.success`, `dedicatedaccount.assign.success`)
were **rejected with 400** — the two 400s in the prod log. The account number
those webhooks carry never landed, so the VA stayed `pending_assign` and the app
polled `GET /wallet/virtual-account` forever.

The 400 came from `webhooks.py`'s generic guard `if not event_id: 400 "Missing
data.id"`, which runs **before** the DVA handler. Real Paystack DVA events carry
**no top-level `data.id`** (only nested `data.customer.id` /
`data.dedicated_account.id`) — the handler itself keys off `customer_code`,
proving it. The bug hid because the **fake fabricated a `data.id`**
(`fake.py`), so every DVA endpoint test passed against a shape Paystack never
sends. Classic test-double-more-generous-than-reality.

Aggravating: nothing requeried a stuck `pending_assign`, so a single missed
webhook hung provisioning permanently (no self-healing).

## How
- **Webhook**: define `_DVA_LIFECYCLE_EVENTS`; when an event of that type lacks
  `data.id`, synthesize a stable dedupe key `f"{event_type}:{customer_code}:{da_id}"`
  so (a) it's no longer rejected and (b) Paystack retries still de-dupe at the
  `WebhookEvent` unique constraint. Charge events still require `data.id`.
- **Fake**: drop the fabricated top-level `data.id` from
  `customer_identification_event` / `dedicated_account_assign_event`; the
  assign event's `event_id` now maps to the (real) `dedicated_account.id`.
- **Recovery**: new `PaymentProvider.fetch_customer_dedicated_account(customer_code)`
  (real client GETs `/customer/{code}` and reads its embedded `dedicated_account`;
  fake has a `will_have_dedicated_account` hook). New Celery task
  `reconcile_pending_dva_assign` sweeps VAs stuck `pending_identity`/`pending_assign`
  past a 180s grace, requeries, and activates them. Row-locked so it never races
  or double-writes vs the live webhook. Beat entry `reconcile-dva-assign-every-2min`.

## What's involved
- `app/api/v1/endpoints/webhooks.py` — `_DVA_LIFECYCLE_EVENTS`, event_id synthesis.
- `app/integrations/paystack/{base,client,fake}.py` — `fetch_customer_dedicated_account`.
- `app/workers/tasks/reconcile_tasks.py` — `reconcile_pending_dva_assign` + `_reconcile_dva_assign`.
- `app/workers/celery_app.py` — beat entry.
- Tests: `tests/api/test_webhooks_dva.py` (now real-shape), `tests/workers/test_reconcile_dva_assign.py` (new).

## Verification
- TDD: making the fake faithful turned the existing DVA webhook tests RED (400),
  proving the bug; the endpoint fix turned them GREEN. `test_webhooks_dva.py` 9/9.
- New recovery tests 5/5 (`test_reconcile_dva_assign.py`): backfill from Paystack,
  pending_identity recovery, "no account yet" left pending, grace-window skip,
  active untouched.
- Affected areas (webhooks/paystack/wallet/reconcile/funding e2e): 85 passed.
- Ruff clean; celery task + beat entry registered.
- (3 unrelated `tests/core/test_config_*` failures locally are caused by a stray
  dev `.env` overriding config defaults — they pass in CI.)

## Operate / recover the stranded account
After deploy, `reconcile_pending_dva_assign` runs every 2 min: it will requery
Paystack for the stuck customer_code and activate the VA automatically (no manual
action). If a customer has multiple Paystack DVAs (e.g. an old deactivated one),
the sweep takes the account Paystack returns on `/customer/{code}`; if that is
stale, the user can now re-provision cleanly since the webhook path works.

## Follow-ups
- **Mobile**: the app still polls with no timeout/failed-state/retry — a spinner
  can still run long if Paystack is slow. Add a poll timeout → failed state →
  "Try again". (Separate `timpbills` repo; deferred per scope.)
- **Issue #2** (separate): Paystack hosted checkout "Couldn't load checkout" on
  the OPay channel is a Paystack-side/client load failure, not our backend.
