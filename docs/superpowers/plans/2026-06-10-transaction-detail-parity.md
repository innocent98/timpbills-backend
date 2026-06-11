# Transaction Detail Page Parity — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bring `/platform-admin/transactions/[ref]` to parity with the `04 _ Transaction detail.png` handoff — a manual-refund panel, an enriched Customer card (wallet balance + member-since), a status-driven hero banner, and real-field-only Transaction data.

**Architecture:** A small backend addition extends the txn-detail `user` payload with `wallet_balance` + `created_at`. The frontend retypes the (previously mis-typed) `triggerRefund` client method, then rebuilds the detail page's right column into a Refund/Status panel that wires the existing manual-refund + requery endpoints, plus hero/customer/data polish. Fields not captured in the data (meter/channel/idempotency) are omitted, never fabricated.

**Tech Stack:** Python / FastAPI / SQLAlchemy + pytest (backend); Next.js 16 / React 19 (frontend).

**Repos:** Backend tasks in `/Users/adebayovictor/Documents/mobile/timp/timpbills-backend` (branch `develop`). Frontend tasks in `/Users/adebayovictor/Documents/mobile/timp/timpbills-marketing` (branch `main`). Commit per repo. No `Co-Authored-By` / AI-attribution trailers. Frontend verification is `npx next build` (no JS test runner).

---

## Task 1: Backend — wallet_balance + created_at on txn-detail user payload

**Files:**
- Modify: `app/services/admin_service.py` (`get_transaction_detail`, ~lines 364-396)
- Test: `tests/api/test_admin_transactions.py`

- [ ] **Step 1: Write the failing tests**

In `tests/api/test_admin_transactions.py`, add `assert`s to the existing `test_transaction_detail_returns_events_and_user` (after `assert "events" in d`):

```python
    assert d["user"]["wallet_balance"] == "0.00"  # no wallet row seeded -> zero
    assert isinstance(d["user"]["created_at"], str) and d["user"]["created_at"]
```

And add a new test at the end of the file:

```python
@pytest.mark.asyncio
async def test_transaction_detail_user_wallet_balance(admin_ctx, login_admin):
    from app.db.models.wallet import Wallet
    client, db, _redis = admin_ctx
    await login_admin()
    u = _seed_user(db)
    db.add(Wallet(user_id=u.id, balance=Decimal("12400.00"))); db.commit()
    tx = _seed_tx(db, u.id, type_=TransactionType.airtime, status=TransactionStatus.success, amount="1000")
    r = await client.get(f"/api/v1/admin/transactions/{tx.reference}")
    assert r.status_code == 200
    assert r.json()["data"]["user"]["wallet_balance"] == "12400.00"
```

(`Decimal` is already imported at the top of this test file.)

- [ ] **Step 2: Run to verify failure**

Run: `poetry run pytest tests/api/test_admin_transactions.py -k "detail" -v --no-cov`
Expected: the two detail tests FAIL on `KeyError: 'wallet_balance'`.

- [ ] **Step 3: Implement**

In `app/services/admin_service.py`, inside `get_transaction_detail`, after the `payment = (...).first()` block and before the `return {`, add:

```python
        wallet = (
            self._db.query(Wallet)
            .filter(Wallet.user_id == tx.user_id)
            .first()
        )
```

Then extend the `user` dict (the `None if user is None else {...}`) with two keys:

```python
            "user": None if user is None else {
                "id": str(user.id), "full_name": user.full_name,
                "email": user.email, "phone": user.phone,
                "kyc_tier": user.kyc_level.numeric,
                "wallet_balance": f"{(wallet.balance if wallet else 0):.2f}",
                "created_at": user.created_at.isoformat(),
            },
```

(`Wallet` is already imported at `admin_service.py:25`.)

- [ ] **Step 4: Run to verify pass**

Run: `poetry run pytest tests/api/test_admin_transactions.py -k "detail" -v --no-cov`
Expected: all detail tests PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/admin_service.py tests/api/test_admin_transactions.py
git commit -m "feat(admin): txn detail user payload carries wallet balance + created_at"
```

---

## Task 2: Frontend — admin-api types (user fields + triggerRefund retype)

**Files:**
- Modify: `lib/admin-api.ts`

- [ ] **Step 1: Extend `TxnDetailUser`**

In `lib/admin-api.ts`, in `export interface TxnDetailUser`, add after `kyc_tier: number;`:

```ts
  /** Money string, e.g. "12400.00". */
  wallet_balance: string;
  created_at: string;
```

- [ ] **Step 2: Add `RefundTriggerResult` + fix `triggerRefund`**

Add this interface near the other domain types (e.g. just below `RefundItem`):

```ts
/** Result of POST /admin/refunds/{ref}/trigger. */
export interface RefundTriggerResult {
  transaction_reference: string;
  transaction_status: string;
  refund_reference: string;
  /** Money string. */
  refund_amount: string;
  was_created: boolean;
}
```

Change the `triggerRefund` method's return type from `RefundItem` to `RefundTriggerResult`:

```ts
  triggerRefund: (ref: string, reason: string) =>
    call<RefundTriggerResult>(`/admin/refunds/${encodeURIComponent(ref)}/trigger`, {
      method: "POST",
      body: JSON.stringify({ reason }),
    }),
```

(Safe: the only other caller, `refunds/page.tsx:92`, `await`s without reading the return value.)

- [ ] **Step 3: Type-check**

Run (in `timpbills-marketing`): `npx tsc --noEmit`
Expected: no errors.

- [ ] **Step 4: Commit**

```bash
git add lib/admin-api.ts
git commit -m "feat(admin-ui): txn detail user wallet/created_at types; fix triggerRefund return type"
```

---

## Task 3: Frontend — Refund/Status panel (right column)

**Files:**
- Modify: `app/platform-admin/transactions/[ref]/page.tsx`

- [ ] **Step 1: Add refundable constant + state + handler**

Below the existing `const REQUERYABLE = new Set(["pending", "processing"]);` line, add:

```tsx
const REFUNDABLE_TYPES = new Set(["airtime", "data", "electricity", "cable", "flight"]);
```

Inside `TransactionDetailPage`, below the existing `requeryNote` state, add:

```tsx
  const [refunding, setRefunding] = useState(false);
  const [refundNote, setRefundNote] = useState<{ tone: "ok" | "err"; text: string } | null>(null);
  const [reason, setReason] = useState("");
```

Below the existing `onRequery` function, add:

```tsx
  async function onRefund() {
    setRefunding(true);
    setRefundNote(null);
    try {
      const res = await adminApi.triggerRefund(ref, reason.trim() || "Manual refund via admin console");
      setRefundNote({
        tone: "ok",
        text: res.was_created
          ? `Refund issued · ₦${fmtMoney(res.refund_amount)} credited to wallet.`
          : "A refund already exists for this transaction — no action taken.",
      });
      reload();
    } catch (e) {
      setRefundNote({ tone: "err", text: errorInfo(e).message });
    } finally {
      setRefunding(false);
    }
  }
```

- [ ] **Step 2: Replace the right-column "Status" `<Card>` with the Refund/Status panel**

Replace the entire existing requery `<Card>` (the one whose first child is `<div ...>Status</div>`, from `{/* requery panel */}`'s `<Card>` through its closing `</Card>`) with:

```tsx
            {/* refund / status panel */}
            <Card>
              {(() => {
                const isRefundable = REFUNDABLE_TYPES.has(t.type) && (t.status === "failed" || t.status === "success");
                const isRequeryable = REQUERYABLE.has(t.status);
                const primaryBtn: React.CSSProperties = {
                  marginTop: 16, height: 50, width: "100%", borderRadius: 13, border: "none", background: A.primary, color: "#fff",
                  display: "flex", alignItems: "center", justifyContent: "center", gap: 9, fontSize: 14, fontWeight: 700, letterSpacing: -0.2,
                  fontFamily: A.font, boxShadow: `0 10px 22px -8px ${A.primary}66`,
                };
                const secondaryBtn: React.CSSProperties = {
                  marginTop: 10, height: 46, width: "100%", borderRadius: 12, background: A.surface, border: `1px solid ${A.hairStrong}`,
                  color: A.ink2, display: "flex", alignItems: "center", justifyContent: "center", gap: 8, fontSize: 13, fontWeight: 700, fontFamily: A.font,
                };
                if (isRefundable) {
                  return (
                    <>
                      <div style={{ fontSize: 15, fontWeight: 800, color: A.ink, letterSpacing: -0.3 }}>Refund</div>
                      <div style={{ marginTop: 14, padding: "14px", borderRadius: 12, background: A.warnSoft, fontSize: 12.5, color: A.ink2, lineHeight: 1.5 }}>
                        Trigger a manual refund to credit the customer&apos;s wallet <strong style={{ color: A.ink }}>₦{fmtMoney(t.amount)}</strong>. Idempotent — if a refund already exists, this is a no-op.
                      </div>
                      <input
                        aria-label="Refund reason"
                        value={reason}
                        onChange={(e) => setReason(e.target.value)}
                        maxLength={500}
                        placeholder="Reason for manual refund"
                        style={{ marginTop: 12, width: "100%", height: 42, borderRadius: 11, border: `1px solid ${A.hairStrong}`, padding: "0 12px", fontSize: 13, color: A.ink, fontFamily: A.font, outline: "none", background: A.surface, boxSizing: "border-box" }}
                      />
                      <button type="button" onClick={onRefund} disabled={refunding} style={{ ...primaryBtn, cursor: refunding ? "not-allowed" : "pointer", opacity: refunding ? 0.6 : 1 }}>
                        <RefundIcon />
                        {refunding ? "Processing…" : "Trigger manual refund"}
                      </button>
                      <button type="button" onClick={onRequery} disabled={requerying} style={{ ...secondaryBtn, cursor: requerying ? "not-allowed" : "pointer", opacity: requerying ? 0.6 : 1 }}>
                        {requerying ? "Requerying…" : "Requery provider"}
                      </button>
                    </>
                  );
                }
                if (isRequeryable) {
                  return (
                    <>
                      <div style={{ fontSize: 15, fontWeight: 800, color: A.ink, letterSpacing: -0.3 }}>Status</div>
                      <div style={{ marginTop: 14, padding: "14px", borderRadius: 12, background: A.warnSoft, display: "flex", gap: 10 }}>
                        <svg width="18" height="18" viewBox="0 0 18 18" style={{ flexShrink: 0, marginTop: 1 }}>
                          <circle cx="9" cy="9" r="7.5" fill="none" stroke={A.warn} strokeWidth="1.5" />
                          <path d="M9 5.5V9.5M9 12v.5" stroke={A.warn} strokeWidth="1.8" strokeLinecap="round" />
                        </svg>
                        <div style={{ fontSize: 12.5, color: A.ink2, lineHeight: 1.5 }}>
                          Awaiting provider confirmation. Requery to pull the latest status from the provider.
                        </div>
                      </div>
                      <button type="button" onClick={onRequery} disabled={requerying} style={{ ...primaryBtn, cursor: requerying ? "not-allowed" : "pointer", opacity: requerying ? 0.6 : 1 }}>
                        <RefundIcon />
                        {requerying ? "Requerying…" : "Requery provider"}
                      </button>
                    </>
                  );
                }
                return (
                  <>
                    <div style={{ fontSize: 15, fontWeight: 800, color: A.ink, letterSpacing: -0.3 }}>Status</div>
                    <div style={{ marginTop: 14, padding: "14px", borderRadius: 12, background: t.status === "refunded" ? A.infoSoft : A.successSoft, fontSize: 12.5, color: A.ink2, lineHeight: 1.5 }}>
                      This transaction is <strong style={{ color: A.ink }}>{humanize(t.status)}</strong>. No further action is available.
                    </div>
                  </>
                );
              })()}
              {refundNote && (
                <div role="status" style={{ marginTop: 12, padding: "10px 12px", borderRadius: 10, fontSize: 12.5, fontWeight: 600, background: refundNote.tone === "ok" ? A.successSoft : A.dangerSoft, color: refundNote.tone === "ok" ? A.success : A.danger }}>
                  {refundNote.text}
                </div>
              )}
              {requeryNote && (
                <div role="status" style={{ marginTop: 12, padding: "10px 12px", borderRadius: 10, fontSize: 12.5, fontWeight: 600, background: requeryNote.tone === "ok" ? A.successSoft : A.dangerSoft, color: requeryNote.tone === "ok" ? A.success : A.danger }}>
                  {requeryNote.text}
                </div>
              )}
            </Card>
```

- [ ] **Step 3: Build**

Run (in `timpbills-marketing`): `npx next build`
Expected: green.

- [ ] **Step 4: Commit**

```bash
git add app/platform-admin/transactions/[ref]/page.tsx
git commit -m "feat(admin-ui): manual refund panel on transaction detail"
```

---

## Task 4: Frontend — hero banner + customer enrichment + meta data row

**Files:**
- Modify: `app/platform-admin/transactions/[ref]/page.tsx`

- [ ] **Step 1: Add helpers**

Below the existing `humanize` function, add:

```tsx
// Status-driven hero banner. Returns null for non-noteworthy statuses.
function heroBanner(t: TxnDetail): { title: string; sub: string | null; tone: "danger" | "warn" | "info" } | null {
  const map: Record<string, { title: string; tone: "danger" | "warn" | "info" }> = {
    failed: { title: "Service delivery failed", tone: "danger" },
    refund_failed: { title: "Refund failed", tone: "danger" },
    refund_pending: { title: "Refund in progress", tone: "warn" },
    refunded: { title: "Refunded", tone: "info" },
  };
  const meta = map[t.status];
  if (!meta) return null;
  const ev = [...t.events].reverse().find((e) => e.reason && (e.to_status === "failed" || e.to_status === t.status));
  return { title: meta.title, sub: ev?.reason ?? null, tone: meta.tone };
}

// ISO timestamp -> "Feb 2026".
function memberSince(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "—" : d.toLocaleDateString("en-US", { month: "short", year: "numeric" });
}
```

- [ ] **Step 2: Render the hero banner**

In the hero `<Card>`, replace the pills row:

```tsx
              <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
                <TypePill type={t.type} />
                <StatusPill status={t.status} />
              </div>
```

with a space-between row that renders the banner on the right:

```tsx
              <div style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between", gap: 12 }}>
                <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
                  <TypePill type={t.type} />
                  <StatusPill status={t.status} />
                </div>
                {(() => {
                  const b = heroBanner(t);
                  if (!b) return null;
                  const c = b.tone === "danger" ? A.danger : b.tone === "warn" ? A.warn : A.info;
                  const bg = b.tone === "danger" ? A.dangerSoft : b.tone === "warn" ? A.warnSoft : A.infoSoft;
                  return (
                    <div style={{ textAlign: "right", maxWidth: 260 }}>
                      <span style={{ display: "inline-block", fontSize: 12, fontWeight: 700, color: c, background: bg, padding: "5px 11px", borderRadius: 8 }}>{b.title}</span>
                      {b.sub && <div style={{ fontSize: 11.5, color: A.muted, marginTop: 5, lineHeight: 1.4 }}>{b.sub}</div>}
                    </div>
                  );
                })()}
              </div>
```

- [ ] **Step 3: Enrich the Customer card**

In the customer card, replace the rows array (currently `[["Phone", ...], ["KYC tier", <TierTag .../>]]`) with:

```tsx
                    {([
                      ["Phone", t.user.phone || "—"],
                      ["KYC tier", <TierTag key="t" tier={t.user.kyc_tier} />],
                      ["Wallet balance", <span key="w" style={{ display: "inline-flex", alignItems: "center" }}><Naira size={12} color={A.muted} />{fmtMoney(t.user.wallet_balance)}</span>],
                      ["Member since", memberSince(t.user.created_at)],
                    ] as [string, React.ReactNode][]).map((r, i, a) => (
```

(The rest of the `.map(...)` body — the row `<div>` with border logic — is unchanged.)

- [ ] **Step 4: Surface a present meta field in Transaction data**

In the "Transaction data" grid array, after the `["Payment status", ...]` entry, add a conditional Provider-txn-id row (only when present and non-empty — seed data often has `""`):

```tsx
                  ...(t.meta && typeof t.meta.vtpass_transaction_id === "string" && t.meta.vtpass_transaction_id
                    ? ([["Provider txn ID", t.meta.vtpass_transaction_id]] as [string, React.ReactNode][])
                    : []),
```

i.e. spread it into the array literal that is `.map`-ed. Ensure the array remains typed `[string, React.ReactNode][]`.

- [ ] **Step 5: Build**

Run (in `timpbills-marketing`): `npx next build`
Expected: green.

- [ ] **Step 6: Commit**

```bash
git add app/platform-admin/transactions/[ref]/page.tsx
git commit -m "feat(admin-ui): hero status banner, customer wallet/member-since, provider txn id"
```

---

## Task 5: Full verification

- [ ] **Step 1: Backend suite + lint**

Run (in `timpbills-backend`):
`poetry run pytest tests/api/test_admin_transactions.py -q --no-cov` (expected: all pass)
`poetry run ruff check app/services/admin_service.py` (expected: clean)

- [ ] **Step 2: Frontend build**

Run (in `timpbills-marketing`): `npx next build` (expected: green, 17 routes)

- [ ] **Step 3 (manual, optional): live smoke**

Backend up + frontend restarted: open a failed bill txn → enter a reason → "Trigger manual refund" → wallet credited, status walks to refunded on reload; verify Customer card shows wallet balance + member-since and the hero banner renders.

---

## Self-review notes

- **Spec coverage:** backend wallet/created_at (Task 1) + types (Task 2); Refund panel with inline reason + idempotent feedback (Task 3); hero banner from real event reason (Task 4 §1-2); customer wallet/member-since (Task 4 §3); real-field-only meta row (Task 4 §4); triggerRefund retype (Task 2). All spec sections covered.
- **Type consistency:** `RefundTriggerResult.refund_amount`/`was_created` consumed exactly in `onRefund`; `REFUNDABLE_TYPES`/`REQUERYABLE` drive the panel branches; `heroBanner`/`memberSince` return types match their render sites; `t.user.wallet_balance`/`created_at` match the Task-1 backend keys and Task-2 type additions.
- **No fabrication:** meter/channel/idempotency omitted; provider-txn-id only rendered when present & non-empty; hero sub-text only from a real event reason.
