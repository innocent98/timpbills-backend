#!/usr/bin/env bash
#
# security-scan.sh - Semgrep SAST gate for Timpbills API.
#
# Runs Semgrep against app/ with five rulesets and blocks on findings of
# the chosen severity or higher. Complements the CI's Trivy scan (Trivy
# checks dependency CVEs; Semgrep checks source-code patterns).
#
# Exit codes:
#   0  clean (no findings at or above --fail-on severity)
#   1  findings present (blocks deploy)
#   2  scan error (blocks deploy - don't silently pass)
#   3  semgrep not installed
#
# Usage:
#   ./scripts/security-scan.sh                 # default: fail on ERROR
#   ./scripts/security-scan.sh --strict        # fail on ERROR or WARNING
#   ./scripts/security-scan.sh --json          # machine-readable output
#
# Intended call sites:
#   - Local:       before `docker compose build`
#   - Pre-commit:  hook on push
#   - CI:          any runner that has bash + python3

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

FAIL_ON="ERROR"
OUTPUT_MODE="human"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --strict)  FAIL_ON="WARNING"; shift ;;
        --json)    OUTPUT_MODE="json"; shift ;;
        -h|--help)
            sed -n '2,22p' "$0"; exit 0 ;;
        *)
            echo "unknown flag: $1" >&2; exit 2 ;;
    esac
done

if ! command -v semgrep >/dev/null 2>&1; then
    echo "semgrep not installed. Install with: brew install semgrep (macOS) or pipx install semgrep" >&2
    exit 3
fi

cd "${PROJECT_ROOT}"

RULESETS=(
    --config=p/python
    --config=p/security-audit
    --config=p/owasp-top-ten
    --config=p/jwt
    --config=p/secrets
)

COMMON_FLAGS=(
    --metrics=off
    --disable-version-check
    --timeout=60
    --severity=ERROR
    --severity=WARNING
)

echo "Running semgrep against app/ (fail-on: ${FAIL_ON})"
echo "    rulesets: python, security-audit, owasp-top-ten, jwt, secrets"
echo

if [[ "${OUTPUT_MODE}" == "json" ]]; then
    semgrep "${RULESETS[@]}" "${COMMON_FLAGS[@]}" --json --quiet app/ > /tmp/semgrep-scan.json
    EXIT=$?
    cat /tmp/semgrep-scan.json
else
    semgrep "${RULESETS[@]}" "${COMMON_FLAGS[@]}" --json --quiet app/ > /tmp/semgrep-scan.json 2>/tmp/semgrep-scan.err
    EXIT=$?
fi

if [[ ${EXIT} -ne 0 && ${EXIT} -ne 1 ]]; then
    echo "semgrep scan error (exit ${EXIT}):" >&2
    cat /tmp/semgrep-scan.err >&2 2>/dev/null || true
    exit 2
fi

export FAIL_ON
python3 - <<'PY'
import json, os, sys
fail_on = os.environ["FAIL_ON"]
with open("/tmp/semgrep-scan.json") as f:
    data = json.load(f)
results = data.get("results", [])
errors = data.get("errors", [])

sev_rank = {"INFO": 0, "WARNING": 1, "ERROR": 2}
gate = sev_rank[fail_on]
blocking = [r for r in results if sev_rank.get(r["extra"]["severity"], 0) >= gate]

from collections import Counter
by_sev = Counter(r["extra"]["severity"] for r in results)
print(f"scanned files: {len(data.get('paths', {}).get('scanned', []))}")
print(f"findings:      {len(results)}  ({dict(by_sev)})")
print(f"blocking:      {len(blocking)}  (severity >= {fail_on})")

if errors:
    print(f"scan errors:   {len(errors)}")
    for e in errors[:5]:
        print(f"  - {e.get('message', '')[:200]}")

if blocking:
    print()
    print("Blocking findings:")
    for r in blocking[:20]:
        rule = r["check_id"].rsplit(".", 1)[-1]
        path = r["path"]
        line = r["start"]["line"]
        msg = r["extra"]["message"].splitlines()[0][:140]
        print(f"  {r['extra']['severity']:7s}  {path}:{line}  {rule}")
        print(f"           {msg}")
    if len(blocking) > 20:
        print(f"  ... and {len(blocking) - 20} more")
    sys.exit(1)

print()
print("no blocking findings")
sys.exit(0)
PY
