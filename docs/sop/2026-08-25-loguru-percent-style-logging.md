# Logs dropped their values: loguru + stdlib %-style args

## What shipped
`app/core/logger.py` — `log` is now a thin `_StdlibStyleLogger` wrapper around
loguru that pre-formats stdlib `%`-style args before emitting. Fixes ~87 log
call sites at once with zero call-site changes.

## Why
The codebase logs stdlib-style — `log.warning("code=%s description=%s", code, desc)`
— but `log` is **loguru**, which formats with `{}` and **silently drops**
positional args, printing the literal `%s`. Real impact: a VTPass live rejection
surfaced in prod as

```
vtpass: purchase failed request_id=%s code=%s description=%s
```

with none of the values — so the actual reason (`code=027 IP NOT WHITELISTED,
CONTACT SUPPORT`) was invisible in our logs and had to be dug out of the VTPass
dashboard. Every `log.*(..., %s, ...)` across auth, KYC, notifications, DVA,
VTPass, etc. was losing its values the same way.

## How
- Wrapper does `msg % args` when positional args are present, then hands loguru a
  finished string via `opt(depth=2).log(level, "{}", msg)`:
  - `depth=2` skips the wrapper frames so the record still points at the real
    caller (`name`/`function`/`line`), verified by test.
  - passing the result as the `{}` arg (not as the template) makes any braces /
    `%%` in the formatted text literal — no accidental re-formatting / KeyError.
  - a bad format string never raises from a log call (falls back to appending
    the args).
- Safe because the codebase has **zero** `{}`-style loguru calls (grep-verified),
  so nothing relied on brace formatting.

Alternative rejected: rewrite all ~87 sites `%s` → `{}`. Large risky diff, and
one missed conversion silently reintroduces the bug. The wrapper is one file and
covers everything, present and future.

## Verification
- `tests/core/test_logger_formatting.py` (4): %-args substituted; no-arg
  braces/`%%` message verbatim; bad format string doesn't raise; call-site
  attribution points at caller not wrapper.
- Full suite: 1004 passed (3 unrelated stray-`.env` config-default failures pass
  in CI). Ruff clean.
- Manual: `log.warning("...code=%s...", "027", ...)` now emits `code=027 ...`
  with correct `file:function:line`.

## Follow-ups
- Unrelated: the VTPass incident that surfaced this needs the prod egress IP
  (`173.249.43.139`, confirm via `curl ifconfig.me` on the VPS) whitelisted on
  the VTPass **live** account — an ops action, not code.
