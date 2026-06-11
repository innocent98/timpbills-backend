# Overview Page Parity + KPI Deltas — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bring `/platform-admin/overview` to full parity with the `02 _ Overview.png` handoff — adding a Recent transactions table, real period-over-period KPI delta badges, and the refund avg sub-line.

**Architecture:** Backend `AdminService.overview()` gains a reusable `_window_metrics(start, end)` helper called for the current and prior windows, returning a new `deltas` block (nullable when the prior window has no data). The frontend overview page renders delta badges on the 3 KPI tiles and adds a Recent-transactions card fed by a second, independent fetch to the existing `/admin/transactions?limit=5` endpoint.

**Tech Stack:** Python / FastAPI / SQLAlchemy + pytest (backend, `timpbills-backend`); Next.js 16 / React 19 / recharts (frontend, `timpbills-marketing`).

**Repos:** Backend tasks run in `/Users/adebayovictor/Documents/mobile/timp/timpbills-backend` (branch `develop`). Frontend tasks run in `/Users/adebayovictor/Documents/mobile/timp/timpbills-marketing` (branch `main`). Commit in each repo independently.

**Note on testing:** The backend is strict TDD (test-first). The marketing repo has no JS test runner configured, so frontend tasks are verified with `npx next build` (type-check + compile) rather than unit tests — this is the established verification path for that repo.

---

## Task 1: Backend — `_window_metrics` helper + `deltas` block

**Files:**
- Modify: `app/services/admin_service.py` (the `overview` method, ~lines 36-117)
- Test: `tests/services/test_admin_overview.py`

- [ ] **Step 1: Add a `created_at`-setting test helper and the delta tests**

Append to `tests/services/test_admin_overview.py` (the file already imports `uuid`, `Decimal`, the enums, `Transaction`, `AdminService`, `new_transaction_reference`):

```python
from datetime import UTC, datetime, timedelta


def _tx_at(db, user_id, *, type_, status, amount, at):
    tx = Transaction(
        user_id=user_id, reference=new_transaction_reference(user_id=str(user_id)),
        type=type_, status=status, amount=Decimal(amount), fee=Decimal("0.00"),
        meta={}, created_at=at,
    )
    db.add(tx); db.commit(); return tx


def test_overview_deltas_compare_prior_window(db_session):
    uid = uuid.uuid4()
    now = datetime.now(UTC)
    prior = now - timedelta(days=10)   # inside [now-14d, now-7d)
    cur = now - timedelta(days=1)      # inside [now-7d, now)
    # prior window: 1 success / 1 failed -> rate 0.5, volume 1000
    _tx_at(db_session, uid, type_=TransactionType.airtime, status=TransactionStatus.success, amount="1000", at=prior)
    _tx_at(db_session, uid, type_=TransactionType.airtime, status=TransactionStatus.failed, amount="500", at=prior)
    # current window: 3 success / 1 failed -> rate 0.75, volume 3000
    _tx_at(db_session, uid, type_=TransactionType.airtime, status=TransactionStatus.success, amount="1000", at=cur)
    _tx_at(db_session, uid, type_=TransactionType.data, status=TransactionStatus.success, amount="1000", at=cur)
    _tx_at(db_session, uid, type_=TransactionType.airtime, status=TransactionStatus.success, amount="1000", at=cur)
    _tx_at(db_session, uid, type_=TransactionType.airtime, status=TransactionStatus.failed, amount="500", at=cur)

    ov = AdminService(db=db_session).overview(days=7)
    # rate 0.75 vs 0.5 -> +25.0 percentage points
    assert ov["deltas"]["success_rate_pp"] == 25.0
    # volume 3000 vs 1000 -> +2.0 (200%) relative fraction
    assert ov["deltas"]["volume_pct"] == 2.0


def test_overview_deltas_refund_total_pct(db_session):
    uid = uuid.uuid4()
    now = datetime.now(UTC)
    _tx_at(db_session, uid, type_=TransactionType.refund, status=TransactionStatus.success, amount="100", at=now - timedelta(days=10))
    _tx_at(db_session, uid, type_=TransactionType.refund, status=TransactionStatus.success, amount="150", at=now - timedelta(days=1))

    ov = AdminService(db=db_session).overview(days=7)
    # refund total 150 vs 100 -> +0.5 (50%)
    assert ov["deltas"]["refund_total_pct"] == 0.5


def test_overview_deltas_null_when_prior_window_empty(db_session):
    uid = uuid.uuid4()
    _tx(db_session, uid, type_=TransactionType.airtime, status=TransactionStatus.success, amount="1000")
    ov = AdminService(db=db_session).overview(days=7)
    assert ov["deltas"]["success_rate_pp"] is None
    assert ov["deltas"]["volume_pct"] is None
    assert ov["deltas"]["refund_total_pct"] is None
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `poetry run pytest tests/services/test_admin_overview.py -v`
Expected: the 3 new tests FAIL with `KeyError: 'deltas'`; `test_overview_counts_and_success_rate` still PASSES.

- [ ] **Step 3: Refactor `overview()` to add the helper + deltas**

In `app/services/admin_service.py`, add this method to the `AdminService` class (place it directly above `overview`):

```python
    def _window_metrics(self, start: datetime, end: datetime) -> dict:
        """Aggregate count/volume/refund metrics for a half-open [start, end) window."""
        base = self._db.query(Transaction).filter(
            Transaction.created_at >= start, Transaction.created_at < end
        )
        total = base.count()
        success_count = base.filter(Transaction.status == _SUCCESS).count()
        volume = (
            self._db.query(func.coalesce(func.sum(Transaction.amount), 0))
            .filter(
                Transaction.created_at >= start, Transaction.created_at < end,
                Transaction.status == _SUCCESS,
            )
            .scalar()
        )
        refund_q = base.filter(Transaction.type == TransactionType.refund)
        refund_count = refund_q.count()
        refund_total = (
            self._db.query(func.coalesce(func.sum(Transaction.amount), 0))
            .filter(
                Transaction.created_at >= start, Transaction.created_at < end,
                Transaction.type == TransactionType.refund,
            )
            .scalar()
        )
        return {
            "transaction_count": total,
            "success_count": success_count,
            "volume": Decimal(volume),
            "refund_total": Decimal(refund_total),
            "refund_count": refund_count,
        }

    @staticmethod
    def _rel_delta(cur: Decimal, prior: Decimal) -> float | None:
        """Relative change (cur-prior)/prior as a float, or None when prior is 0."""
        if prior == 0:
            return None
        return float((cur - prior) / prior)
```

Now replace the body of `overview()` down to (but NOT including) the `# service mix` block. Specifically, replace lines that currently compute `q`, `total`, `success_count`, `success_rate`, `volume`, `refund_rows`, `refund_count`, `refund_total` (the block from `q = self._db.query(...)` through the `refund_total = (...).scalar()` statement) with:

```python
        cur = self._window_metrics(since, now)
        prior = self._window_metrics(since - timedelta(days=days), since)

        total = cur["transaction_count"]
        success_rate = round(cur["success_count"] / total, 4) if total else 0.0
        volume = cur["volume"]
        refund_count = cur["refund_count"]
        refund_total = cur["refund_total"]

        prior_rate = (
            prior["success_count"] / prior["transaction_count"]
            if prior["transaction_count"]
            else None
        )
        deltas = {
            "success_rate_pp": (
                round((success_rate - prior_rate) * 100, 1)
                if prior_rate is not None
                else None
            ),
            "volume_pct": self._rel_delta(cur["volume"], prior["volume"]),
            "refund_total_pct": self._rel_delta(cur["refund_total"], prior["refund_total"]),
        }

        # service mix + daily_volume still scan the current window directly.
        q = self._db.query(Transaction).filter(Transaction.created_at >= since)
```

(The reinstated `q = ...` line keeps the existing `service_mix`, `daily_volume`, and `needs_attention` queries below working unchanged — they reference `q`.)

Then add `"deltas": deltas,` to the returned dict, immediately after the `"refund_total_ngn"` line:

```python
            "refund_total_ngn": f"{Decimal(refund_total):.2f}",
            "deltas": deltas,
            "service_mix": service_mix,
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `poetry run pytest tests/services/test_admin_overview.py -v`
Expected: all tests PASS (the original + 3 new).

- [ ] **Step 5: Commit**

```bash
git add app/services/admin_service.py tests/services/test_admin_overview.py
git commit -m "feat(admin): period-over-period deltas on overview metrics"
```

---

## Task 2: Backend — assert `deltas` in the overview API shape test

**Files:**
- Test: `tests/api/test_admin_overview_api.py`

- [ ] **Step 1: Add the assertion**

In `test_overview_authed_returns_metrics`, after the existing `assert "needs_attention" in data` line, add:

```python
    assert "deltas" in data
    assert set(data["deltas"]) == {"success_rate_pp", "volume_pct", "refund_total_pct"}
```

- [ ] **Step 2: Run the test**

Run: `poetry run pytest tests/api/test_admin_overview_api.py -v`
Expected: PASS (with no transactions seeded, the three delta values are `null`, but all three keys are present).

- [ ] **Step 3: Commit**

```bash
git add tests/api/test_admin_overview_api.py
git commit -m "test(admin): assert deltas block in overview API response"
```

---

## Task 3: Frontend — add `deltas` to the `Overview` type

**Files:**
- Modify: `lib/admin-api.ts` (the `Overview` interface, ~lines 128-141)

- [ ] **Step 1: Extend the interface**

In `lib/admin-api.ts`, inside `export interface Overview`, add this field immediately after `refund_total_ngn: string;`:

```ts
  deltas: {
    /** (current_rate − prior_rate) × 100, in percentage POINTS; null if prior window had no transactions. */
    success_rate_pp: number | null;
    /** (cur − prior) / prior relative fraction; null if prior volume was 0. */
    volume_pct: number | null;
    /** (cur − prior) / prior relative fraction; null if prior refund total was 0. */
    refund_total_pct: number | null;
  };
```

- [ ] **Step 2: Type-check**

Run (in `timpbills-marketing`): `npx tsc --noEmit`
Expected: no new errors (the page still compiles; it just doesn't read `deltas` yet).

- [ ] **Step 3: Commit**

```bash
git add lib/admin-api.ts
git commit -m "feat(admin-ui): add deltas to Overview API type"
```

---

## Task 4: Frontend — KPI delta badges + refund avg sub-line

**Files:**
- Modify: `app/platform-admin/overview/page.tsx`

- [ ] **Step 1: Add the `DeltaBadge` component and formatting helpers**

In `app/platform-admin/overview/page.tsx`, add these above the `KpiCard` function:

```tsx
type Tone = "good" | "bad" | "neutral";

function DeltaBadge({ text, tone }: { text: string; tone: Tone }) {
  const map: Record<Tone, { c: string; bg: string }> = {
    good: { c: A.success, bg: A.successSoft },
    bad: { c: A.danger, bg: A.dangerSoft },
    neutral: { c: A.ink2, bg: A.page },
  };
  const { c, bg } = map[tone];
  const up = text.trim().startsWith("+");
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 4, fontSize: 12, fontWeight: 700, color: c, background: bg, padding: "4px 9px", borderRadius: 8 }}>
      <svg width="10" height="10" viewBox="0 0 10 10" aria-hidden>
        {up ? <path d="M5 1.5l3.5 4H6.4v3H3.6v-3H1.5L5 1.5z" fill={c} /> : <path d="M5 8.5l-3.5-4H3.6v-3h2.8v3H8.5L5 8.5z" fill={c} />}
      </svg>
      {text}
    </span>
  );
}

// Percentage-points delta (success rate). null -> no badge.
function ppDelta(v: number | null): { text: string; tone: Tone } | undefined {
  if (v === null) return undefined;
  const sign = v > 0 ? "+" : v < 0 ? "−" : "";
  return { text: `${sign}${Math.abs(v).toFixed(1)}pp`, tone: v >= 0 ? "good" : "bad" };
}

// Relative-percent delta. `lowerIsBetter` flips the good/bad colour (refunds).
function pctDelta(v: number | null, lowerIsBetter = false): { text: string; tone: Tone } | undefined {
  if (v === null) return undefined;
  const pct = Math.round(v * 100);
  if (pct === 0) return { text: "0%", tone: "neutral" };
  const sign = pct > 0 ? "+" : "−";
  const positive = pct > 0;
  const tone: Tone = positive !== lowerIsBetter ? "good" : "bad";
  return { text: `${sign}${Math.abs(pct)}%`, tone };
}

// Compact naira magnitude for the refund avg sub-line: 6000 -> "6k", 1_200_000 -> "1.2M".
function fmtCompact(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(n % 1_000_000 === 0 ? 0 : 1)}M`;
  if (n >= 1_000) return `${Math.round(n / 1000)}k`;
  return `${Math.round(n)}`;
}

// "162 refunds · ₦6k avg" — omits the avg segment when count is 0.
function refundTarget(count: number, totalNgn: string): string {
  const base = `${count.toLocaleString("en-US")} refund${count === 1 ? "" : "s"}`;
  if (count === 0) return base;
  return `${base} · ₦${fmtCompact(Number(totalNgn) / count)} avg`;
}
```

- [ ] **Step 2: Give `KpiCard` an optional `delta` prop and render it top-right of the icon row**

Change the `KpiCard` signature to accept `delta`:

```tsx
function KpiCard({ label, value, target, icon, delta }: { label: string; value: React.ReactNode; target: string; icon: KpiIcon; delta?: { text: string; tone: Tone } }) {
```

Replace the icon-row `<div>` (currently `style={{ display: "flex", alignItems: "center", marginBottom: 18 }}`) with a space-between row that also renders the badge:

```tsx
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 18 }}>
        <div style={{ width: 40, height: 40, borderRadius: 11, background: A.primarySoft, display: "flex", alignItems: "center", justifyContent: "center" }}>
          <svg width="22" height="22" viewBox="0 0 22 22">{icons[icon]}</svg>
        </div>
        {delta && <DeltaBadge text={delta.text} tone={delta.tone} />}
      </div>
```

- [ ] **Step 3: Pass deltas + refund avg into the three KPI cards**

In the KPI grid, update the three `<KpiCard>` usages:

```tsx
            <KpiCard
              label="Transaction success rate"
              value={`${(data.success_rate * 100).toFixed(1)}%`}
              target={`${data.transaction_count.toLocaleString("en-US")} transactions`}
              icon="check"
              delta={ppDelta(data.deltas.success_rate_pp)}
            />
            <KpiCard
              label="Total volume (7d)"
              value={<><Naira size={24} color={A.muted} />{fmtMoney(data.volume_ngn)}</>}
              target={`${data.transaction_count.toLocaleString("en-US")} transactions`}
              icon="chart"
              delta={pctDelta(data.deltas.volume_pct)}
            />
            <KpiCard
              label="Refunds (7d)"
              value={<><Naira size={24} color={A.muted} />{fmtMoney(data.refund_total_ngn)}</>}
              target={refundTarget(data.refund_count, data.refund_total_ngn)}
              icon="refund"
              delta={pctDelta(data.deltas.refund_total_pct, true)}
            />
```

- [ ] **Step 4: Type-check + build**

Run (in `timpbills-marketing`): `npx next build`
Expected: build succeeds, no type errors.

- [ ] **Step 5: Commit**

```bash
git add app/platform-admin/overview/page.tsx
git commit -m "feat(admin-ui): KPI delta badges + refund avg on overview"
```

---

## Task 5: Frontend — Recent transactions table + 2-col bottom row

**Files:**
- Modify: `app/platform-admin/overview/page.tsx`

- [ ] **Step 1: Extend imports**

At the top of `app/platform-admin/overview/page.tsx`, update the imports:

```tsx
import { Card, Naira, SectionTitle, fmtMoney, TypePill, StatusPill, Avatar } from "../components/primitives";
import { LoadingState, ErrorState, EmptyState, useAdminResource, initials, fmtAge } from "../components/states";
import { adminApi, type Overview, type Paginated, type TxnListItem } from "@/lib/admin-api";
```

- [ ] **Step 2: Add the `RecentTransactions` component**

Add above `OverviewPage`:

```tsx
function RecentTransactions({ loading, error, data, onRetry }: {
  loading: boolean; error: Error | null; data: Paginated<TxnListItem> | null; onRetry: () => void;
}) {
  return (
    <Card>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 6 }}>
        <SectionTitle title="Recent transactions" />
        <Link href="/platform-admin/transactions" style={{ fontSize: 12, fontWeight: 700, color: A.primary, textDecoration: "none" }}>View all</Link>
      </div>
      {loading ? (
        <LoadingState label="Loading…" minHeight={240} />
      ) : error ? (
        <ErrorState error={error} onRetry={onRetry} minHeight={240} />
      ) : !data || data.items.length === 0 ? (
        <EmptyState title="No transactions yet" sub="Nothing has been recorded." minHeight={240} />
      ) : (
        <div>
          {data.items.map((r) => (
            <Link
              key={r.reference}
              href={`/platform-admin/transactions/${encodeURIComponent(r.reference)}`}
              style={{ display: "flex", alignItems: "center", gap: 12, padding: "11px 4px", borderBottom: `1px solid ${A.hair}`, textDecoration: "none" }}
            >
              <span style={{ width: 104, fontFamily: A.display, fontSize: 13, fontWeight: 700, color: A.ink, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>#{r.reference}</span>
              <div style={{ width: 116 }}><TypePill type={r.type} /></div>
              <div style={{ flex: 1, minWidth: 0, display: "flex", alignItems: "center", gap: 9 }}>
                <Avatar initials={initials(r.customer_name)} size={28} />
                <span style={{ fontSize: 13, fontWeight: 600, color: A.ink, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>{r.customer_name}</span>
              </div>
              <span style={{ display: "flex", alignItems: "center", fontSize: 13, fontWeight: 700, color: A.ink }}><Naira size={13} color={A.muted} />{fmtMoney(r.amount)}</span>
              <div style={{ width: 104, paddingLeft: 16 }}><StatusPill status={r.status} /></div>
              <span style={{ width: 38, textAlign: "right", fontSize: 12, color: A.muted }}>{fmtAge(r.created_at)}</span>
            </Link>
          ))}
        </div>
      )}
    </Card>
  );
}
```

- [ ] **Step 3: Add the second fetch in `OverviewPage`**

Inside `OverviewPage`, just below the existing `useAdminResource<Overview>(...)` line, add:

```tsx
  const txFetcher = useCallback(() => adminApi.transactions("limit=5"), []);
  const recent = useAdminResource<Paginated<TxnListItem>>(txFetcher, "overview:recent-txns");
```

- [ ] **Step 4: Replace the full-width Needs-attention block with a 2-col row**

Replace the existing `{/* needs attention — real counts only */}` wrapper `<div style={{ marginTop: 18 }}>` ... `</div>` with a 2-column grid whose first cell is the Recent transactions card and second cell is the existing Needs-attention `<Card>` (move the Needs-attention `<Card>...</Card>` JSX verbatim into the second cell):

```tsx
          {/* recent transactions + needs attention */}
          <div style={{ display: "grid", gridTemplateColumns: "1.6fr 1fr", gap: 18, marginTop: 18 }}>
            <RecentTransactions loading={recent.loading} error={recent.error} data={recent.data} onRetry={recent.reload} />
            <Card>
              <div style={{ fontSize: 15, fontWeight: 800, color: A.ink, letterSpacing: -0.3, marginBottom: 16 }}>Needs attention</div>
              {/* ...existing needs_attention conditional body, unchanged... */}
            </Card>
          </div>
```

(Keep the existing Needs-attention conditional body — the `data.needs_attention.refunds_awaiting === 0 && ...` block — exactly as it was; only its wrapping `<div style={{ marginTop: 18 }}>` is replaced by this grid cell.)

- [ ] **Step 5: Build**

Run (in `timpbills-marketing`): `npx next build`
Expected: build succeeds, no type errors.

- [ ] **Step 6: Commit**

```bash
git add app/platform-admin/overview/page.tsx
git commit -m "feat(admin-ui): recent transactions table + 2-col overview layout"
```

---

## Task 6: Full verification

- [ ] **Step 1: Backend full suite**

Run (in `timpbills-backend`): `poetry run pytest -q`
Expected: all tests pass (was 780; now 783 with the 3 new delta tests).

- [ ] **Step 2: Backend lint**

Run (in `timpbills-backend`): `poetry run ruff check app/services/admin_service.py`
Expected: no findings.

- [ ] **Step 3: Frontend build**

Run (in `timpbills-marketing`): `npx next build`
Expected: green build, all routes compile.

- [ ] **Step 4 (manual, optional): live smoke test**

With the backend running locally (`ADMIN_COOKIE_SECURE=False`, `ADMIN_COOKIE_DOMAIN` unset, CORS allowing the dashboard origin with credentials) and `NEXT_PUBLIC_ADMIN_API_BASE` pointed at it, log into `/platform-admin`, open Overview, and confirm: KPI cards render (delta badges appear only once two 7-day windows of data exist), the Recent transactions card lists the latest 5 with working row links and "View all", and Needs attention sits to its right.

---

## Self-review notes

- **Spec coverage:** deltas backend (Task 1) + type (Task 3) + badges (Task 4); recent transactions (Task 5); refund avg (Task 4); 2-col layout (Task 5); null rules (Task 1 tests); API shape (Task 2). All spec sections covered.
- **Type consistency:** `Tone` used uniformly; `ppDelta`/`pctDelta` return `{text; tone}` matching `KpiCard.delta`; `_window_metrics` keys (`transaction_count`, `success_count`, `volume`, `refund_total`, `refund_count`) are the exact keys consumed in `overview()`.
- **Out of scope (intentional):** the success/volume KPI sub-lines keep their existing `N transactions` text (no fabricated "Target ≥ 97%"); volume `BarChart` and Service-mix bars untouched.
