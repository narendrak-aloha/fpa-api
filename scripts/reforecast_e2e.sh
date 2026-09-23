#!/usr/bin/env bash
# One re-forecast from shock to commitment, with no UI: start it as the planner,
# wait for it to park, record the covenant as the controller, decide as the cfo,
# wait for the end, then show the Commitment Service ledger.
#   scripts/reforecast_e2e.sh                          # approve, commit
#   FAIL=1 scripts/reforecast_e2e.sh                   # commitment service down: see the rollback
#   DECISION=reject scripts/reforecast_e2e.sh          # reject instead
# PLAN, DRIVER, FROM, TO, API_URL and COMMITMENT_URL override the defaults.
set -euo pipefail

PLAN=${PLAN:-PV-2026-0001}
DRIVER=${DRIVER:-utilisation}
FROM=${FROM:-0.75}
TO=${TO:-0.70}
DECISION=${DECISION:-approve}
FAIL=${FAIL:-0}
API_URL=${API_URL:-localhost:8000/api/v1}
COMMITMENT_URL=${COMMITMENT_URL:-localhost:8100}
TIMEOUT=${TIMEOUT:-600}

api() { # api TOKEN METHOD PATH [BODY]
  curl -sS --fail-with-body -H 'content-type: application/json' -H "Authorization: Bearer $1" \
    -X "$2" "$API_URL$3" ${4:+-d "$4"}
}
field() { python3 -c "import sys,json; print(json.load(sys.stdin).get('$1') or '')"; }

# Waits until the run's phase is one of the given ones; prints it.
wait_phase() {
  local deadline=$((SECONDS + TIMEOUT)) phase="" seen=""
  while ((SECONDS < deadline)); do
    phase=$(api tok-planner GET "/reforecast/$PLAN/progress" 2>/dev/null | field phase || true)
    for want in "$@"; do [[ $phase == "$want" ]] && { echo "$phase"; return 0; }; done
    [[ $phase != "$seen" ]] && { echo "   phase: ${phase:-?}" >&2; seen=$phase; }
    sleep 2
  done
  echo "timed out after ${TIMEOUT}s waiting for $* (last phase: ${phase:-none})" >&2
  return 1
}

restore_commitment() { curl -sS -X POST "$COMMITMENT_URL/admin/failure-rate" -H 'content-type: application/json' -d '{"rate":0.0}' >/dev/null || true; }
[[ $FAIL == 1 ]] && trap restore_commitment EXIT

echo "==> $PLAN: shock $DRIVER $FROM -> $TO (planner)"
api tok-planner POST /reforecast \
  "{\"plan_version_code\":\"$PLAN\",\"driver_code\":\"$DRIVER\",\"from_value\":$FROM,\"to_value\":$TO}" | python3 -m json.tool

echo "==> waiting for the run to park"
phase=$(wait_phase AWAITING_APPROVAL DONE FAILED)
if [[ $phase != AWAITING_APPROVAL ]]; then
  echo "run ended before approval ($phase)"; api tok-planner GET "/reforecast/$PLAN/progress" | python3 -m json.tool; exit 1
fi

successor=$(api tok-planner GET "/reforecast/$PLAN/progress" | field target_version_code)
echo "==> covenant pass on $successor (controller)"
row_version=$(api tok-controller GET "/plan-versions/$successor" | field row_version)
api tok-controller PUT "/plan-versions/$successor/covenant" \
  "{\"expected_version\":$row_version,\"covenant_ok\":true,\"note\":\"covenant reviewed (e2e)\"}" >/dev/null

if [[ $FAIL == 1 ]]; then
  echo "==> commitment service set to fail every call"
  curl -sS -X POST "$COMMITMENT_URL/admin/failure-rate" -H 'content-type: application/json' -d '{"rate":1.0,"mode":"error"}' >/dev/null
fi

approved=$([[ $DECISION == reject ]] && echo false || echo true)
echo "==> $DECISION (cfo)"
api tok-cfo POST "/reforecast/$PLAN/decision" "{\"approved\":$approved,\"comment\":\"$DECISION (e2e)\"}" >/dev/null

echo "==> waiting for the run to finish"
wait_phase DONE FAILED >/dev/null
echo "==> final progress"
api tok-planner GET "/reforecast/$PLAN/progress" | python3 -m json.tool

echo "==> commitment ledger for $PLAN"
curl -sS "$COMMITMENT_URL/commitments?plan_version=$PLAN" | python3 -c '
import sys, json
from collections import Counter
rows = json.load(sys.stdin)["commitments"]
for r in sorted(rows, key=lambda r: (r["revision"], r["scenario"], r["category"])):
    print("  rev {revision:>3}  {scenario:<9} {category:<8} {amount:>18}  {state}".format(**r))
print("  " + ", ".join(f"rev {rev} {state}: {n}" for (rev, state), n in sorted(Counter((r["revision"], r["state"]) for r in rows).items())))
'
