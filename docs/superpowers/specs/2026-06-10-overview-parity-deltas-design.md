# Overview page parity + KPI deltas — design

**Date:** 2026-06-10
**Scope:** Cross-stack — `timpbills-backend` (admin overview endpoint) + `timpbills-marketing` (`/platform-admin/overview` page).
**Status:** Approved, pending implementation plan.

## Goal

Bring `/platform-admin/overview` to full visual + data parity with the
`02 _ Overview.png` design handoff, and enrich it with more recent data:

1. **Recent transactions** table (the "more recent data" ask).
2. **KPI delta badges** (period-over-period), backed by real data.
3. Refund KPI **avg** sub-line.

Deliberately **out of scope** (no fabricated metrics — project rule): the
"Avg processing time" KPI tile (not persisted) and the "Amadeus latency
elevated" alert (flights not built). The volume `BarChart` and Service-mix
bars already match the design and are unchanged.

## Background / current state

- The volume chart (`VolumeChart.tsx`) already uses recharts (`BarChart` +
  `Tooltip` + `ResponsiveContainer`) and is already interactive. No charting
  work is needed.
- The overview endpoint (`AdminService.overview`) returns absolute,
  current-window-only metrics — no period-over-period comparison.
- The `/admin/transactions` endpoint already exists and supports `limit`;
  the overview page simply does not call it today.

## Backend — `app/services/admin_service.py`

### Refactor
Extract the current single-window aggregation (success_rate, volume,
refund_total) into a private helper `_window_metrics(start, end) -> dict`
returning at least `{transaction_count, success_count, volume, refund_total}`.
Call it twice:
- **current** window `[now - days, now)`
- **prior**   window `[now - 2*days, now - days)`

`service_mix`, `daily_volume`, and `needs_attention` remain
current-window-only and are NOT refactored into the helper.

### New response field: `deltas`
Each value is a number **or `null`**:

```jsonc
"deltas": {
  "success_rate_pp":  0.6,   // (cur_rate − prior_rate) × 100, rounded to 1dp — percentage POINTS
  "volume_pct":       0.12,  // (cur − prior) / prior — relative fraction
  "refund_total_pct": 0.04   // (cur − prior) / prior — relative fraction, on the ₦ headline
}
```

### Null rules (load-bearing for a young dataset)
- `volume_pct` → `null` when prior-window volume is `0` (zero denominator).
- `refund_total_pct` → `null` when prior-window refund total is `0`.
- `success_rate_pp` → `null` when the prior window had `0` transactions
  (rate undefined, so the delta is meaningless rather than `current − 0`).

Computed deltas are JSON numbers; `volume_pct`/`refund_total_pct` are
unrounded fractions (frontend formats), `success_rate_pp` is rounded to 1dp.

### Tests (`tests/.../test_admin_overview*`)
- Prior-window-zero → `null` for each of the three deltas.
- Normal positive delta and normal negative delta.
- Refund-up case (positive `refund_total_pct`).
- Update the existing overview-shape test to assert the new `deltas` key.

## Frontend — `lib/admin-api.ts`

Add to the `Overview` interface:
```ts
deltas: {
  success_rate_pp: number | null;
  volume_pct: number | null;
  refund_total_pct: number | null;
};
```

## Frontend — `app/platform-admin/overview/page.tsx`

### 1. KPI delta badges
Extend `KpiCard` with an optional prop:
```ts
delta?: { text: string; tone: "good" | "bad" | "neutral" }
```
Rendered as a pill at the **top-right of the icon row** (matches the mock).
`null` delta → render **no badge** (not "+0%" / "—").

**Tone mapping (decisions locked):**
- Success rate & volume: positive → `good` (green), negative → `bad` (red).
- Refunds: positive → `bad` (red), negative → `good` (green) — refunds are
  lower-is-better.

**Text format:**
- Success rate: percentage points — `+0.6pp` / `−0.4pp`.
- Volume & refunds: relative percent — `+12%` / `−5%`.
- Explicit `+`/`−` sign and a direction arrow, colored by tone, so a falling
  success rate is unambiguous.

### 2. Refund KPI sub-line
Append a derived average: `162 refunds · ₦6k avg`
(`refund_total_ngn / refund_count`, compact-rounded). Omit the `· avg`
segment when `refund_count` is 0.

### 3. Recent transactions table (new section)
- A **second, independent** fetch via `adminApi.transactions("limit=5")`
  using its own `useAdminResource` (separate loading / error / empty state —
  it can fail or be empty without blanking the overview).
- Rows reuse existing primitives, mirroring the transactions-list row,
  condensed: `#reference` (display font) · `TypePill` · `Avatar`+`initials`
  + customer name · `Naira`+`fmtMoney` amount · `StatusPill` · `fmtAge`
  relative time.
- Each row links to `/platform-admin/transactions/{reference}`.
- Card header: title "Recent transactions" + a `View all` `Link` →
  `/platform-admin/transactions`.

### 4. Bottom-row layout
Replace the current full-width "Needs attention" card with a 2-column grid
`gridTemplateColumns: "1.6fr 1fr"` (Recent transactions | Needs attention),
mirroring the charts row above it and matching the mock.

## Reused, not rebuilt
- Primitives: `Card`, `Naira`, `fmtMoney`, `TypePill`, `StatusPill`,
  `Avatar`, `SectionTitle`.
- Helpers from `states.tsx`: `initials`, `fmtAge`, `useAdminResource`,
  `LoadingState` / `ErrorState` / `EmptyState`.

## Verification
- Backend: `pytest` for delta math + null edges; full suite stays green.
- Frontend: `npx next build` green.
- Manual: run against the local backend. Local-dev prereqs still apply —
  `ADMIN_COOKIE_SECURE=False`, `ADMIN_COOKIE_DOMAIN` unset, backend CORS
  allows the dashboard origin **with credentials**.

## Risks / notes
- Two independent fetches on one page is the accepted tradeoff of the
  "separate call" decision; recent-txns renders its own small state.
- This touches the live admin overview contract (additive only — existing
  consumers unaffected since `deltas` is a new key).
