# VTPass Sandbox Integration Verification — Request IDs

## What shipped
End-to-end live verification of the VTPass bill-payment integration against the
**VTPass sandbox** (`https://sandbox.vtpass.com`) via the **staging** backend
(`https://staging-api.timpbills.com`). 23 successful transactions across every
supported service type, each producing a VTPass `request_id`. No code change —
this is the durable record proving the integration works against the real
sandbox, with the exact `request_id` per service.

Verified 2026-08-15 on staging. Test account: phone `09066128757`
(user `fc201e42-…`).

## Why
Needed the "Request ID of a successful integration" for each network / disco /
cable provider — proof the wire path (our backend → VTPass `/api/pay`) returns
`code=000 TRANSACTION SUCCESSFUL` and that we persist a compliant `request_id`.

Key fact about `request_id`: in this integration the VTPass `request_id` **is**
the transaction reference (`tx.reference`), passed straight through at
`app/services/bill_service.py` (`provider_fn(tx.reference)`). The reference is
minted by `app/utils/references.py` as `{YYYYMMDDHHMM}TMP{user6}{ULID10}`, which
satisfies VTPass's rule that `request_id` must begin with 12 numeric chars of
Africa/Lagos time. Do **not** confuse it with the API envelope's own
`request_id` (a per-HTTP-call id, unrelated to VTPass).

## How
1. Login `POST /auth/login` `{phone, password}` → access token.
2. `POST /auth/pin/verify` `{pin}` → `X-Pin-Token` (money-ops, 5-min TTL).
3. Wallet was ₦6,000; full run needs ~₦10.5k. Topped up +₦6,000 via
   `POST /wallet/fund` → Paystack **test** checkout (simulated "Success"
   outcome) → `charge.success` webhook credited wallet to ₦12,000.
4. Fired each purchase with `Authorization: Bearer`, `X-Pin-Token`, and a unique
   `Idempotency-Key`.

### Sandbox test values that matter (any other value → simulated failure)
| Service | Field | Success value |
|---|---|---|
| Electricity (prepaid) | meter (`billersCode`) | `1111111111111` (13 ones) |
| Electricity (postpaid) | meter | `1010101010101` |
| Cable (DSTV/GOtv/Startimes) | smartcard | `1212121212` |
| Airtime / Data | phone | any valid MSISDN (used `08011111111`) |

> Gotcha found during the run: an early electricity attempt used a 10-digit
> `1111111111` meter → VTPass returned failed → our **refund-as-row** path
> auto-issued a `type=refund status=success` row (net-zero wallet). Confirms the
> failure/refund path works, but always use the **13-digit** sandbox meter for a
> success.

## Request IDs (the deliverable)

### Airtime — `POST /bills/airtime`
| Network | Amount | Request ID | Status |
|---|---|---|---|
| MTN | ₦50 | `202608150831TMPfc201e6ZA42NRT08` | success |
| Airtel | ₦50 | `202608150831TMPfc201eVM9NPDEWK9` | success |
| Glo | ₦50 | `202608150832TMPfc201eKP69PY7VM3` | success |
| 9mobile (etisalat) | ₦50 | `202608150832TMPfc201e4TGNHS1BA2` | success |

### Data — `POST /bills/data`
| Network | Plan | Request ID | Status |
|---|---|---|---|
| MTN | `mtn-10mb-100` | `202608150837TMPfc201eSSNNKKSTXS` | success |
| Airtel | `airt-50` | `202608150838TMPfc201e8BGYCG5BDB` | success |
| Glo | `glo-wtf-25` | `202608150838TMPfc201eM6T51BQR7N` | success |
| 9mobile (etisalat) | `eti-100` | `202608150838TMPfc201e90F4PXGHKY` | success |

### Electricity — `POST /bills/electricity` (prepaid, meter `1111111111113`… `1111111111111`)
| Disco | Amount | Request ID | Status |
|---|---|---|---|
| Ikeja (IKEDC) | ₦500 | `202608150908TMPfc201e046JE1FR3D` | success |
| Eko (EKEDC) | ₦1,000 | `202608150911TMPfc201eM7AHYSAPAT` | success |
| Abuja (AEDC) | ₦900 | `202608150911TMPfc201eF4HG0BTE2N` | success |
| Kano (KEDCO) | ₦500 | `202608150912TMPfc201eG55V8CDS71` | success |
| Port Harcourt (PHED) | ₦100 | `202608150912TMPfc201eBBBK3PW50P` | success |
| Jos (JED) | ₦1,000 | `202608150917TMPfc201eY2W13C3MY9` | success |
| Kaduna (KAEDCO) | ₦1,100 | `202608150917TMPfc201eM3NBE51DJ6` | success |
| Enugu (EEDC) | ₦500 | `202608150917TMPfc201e0D50M87342` | success |
| Ibadan (IBEDC) | ₦2,000 | `202608150918TMPfc201e6GYHZP92W7` | success |
| Benin (BEDC) | ₦500 | `202608150918TMPfc201eQ6HQY7YGXQ` | success |
| Aba (ABEDC) | ₦10 | `202608150918TMPfc201eA8AQERA3V6` | success |
| Yola (YEDC) | ₦500 | `202608150919TMPfc201e3EQJJRYD93` | success |

### Cable TV — `POST /bills/cable` (mode=change, smartcard `1212121212`)
| Provider | Bouquet | Amount | Request ID | Status |
|---|---|---|---|---|
| DSTV | `dstv-mobile-1` | ₦790 | `202608150922TMPfc201eH51Z2W7Z49` | success |
| GOtv | `gotv-lite` | ₦410 | `202608150923TMPfc201eZ5E4F5B5NV` | success |
| Startimes | `nova-daily` | ₦90 | `202608150923TMPfc201e093QBGW2NT` | success |

## Verification
- Every purchase returned HTTP 200 with `data.status = "success"`.
- Electricity successes returned a decoded `token` + `units` (e.g. Ikeja
  `Token : 26362054405982757802`, `79.9 kWh`).
- Cross-checked against the ledger `GET /transactions` — all 23 rows read
  `status=success`; the two intentional-failure ikeja attempts read `failed`
  with matching `type=refund status=success` rows.
- Wallet moved ₦12,000 → ₦625.01 consistent with the spend.

## Operate / roll back
Nothing to roll back — sandbox test spend only. To re-run: repeat the
login → pin/verify → (fund if needed) → purchase sequence. Live cutover requires
swapping `VTPASS_BASE_URL` to `https://vtpass.com` and the live keys; the request
path is identical.

## Follow-ups
- One duplicate jos-electric purchase occurred (`…0913…Y8G39QY0WY`) because a
  client-side shell timeout killed a curl that had already completed
  server-side. Harmless (both succeeded), but a reminder that a killed client
  does not un-do a VTPass call — rely on the `Idempotency-Key` (a retry with the
  **same** key would have been de-duped; the timeout used a new key).
- Postpaid electricity and cable `renew` mode were not exercised in this run
  (only prepaid + cable `change`).
