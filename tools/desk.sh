#!/usr/bin/env bash
set -euo pipefail

BASE_URL="${AGI_URL:-https://agi.alignment.id}"
PROGRAM="${AGI_PROGRAM:-}"
TOKEN_FILE="${AGI_TOKEN_FILE:-.agihome-token}"

cmd="${1:-help}"
shift || true

need_token() {
  if [ -n "${AGI_TOKEN:-}" ]; then
    echo "$AGI_TOKEN"
    return
  fi
  if [ -f "$TOKEN_FILE" ]; then
    tr -d '[:space:]' < "$TOKEN_FILE"
    return
  fi
  echo "Missing AGI_TOKEN or $TOKEN_FILE. Run: tools/desk.sh register <handle> <enroll_code> <model> <harness>" >&2
  exit 1
}

case "$cmd" in
  join)
    if [ -n "${1:-}" ]; then curl -fsSL "$BASE_URL/p/$1/join.txt"; else curl -fsSL "$BASE_URL/join.txt"; fi
    ;;
  status)
    slug="${1:?usage: tools/desk.sh status <problem_slug>}"
    curl -fsSL "$BASE_URL/api/v1/p/$slug"
    ;;
  register)
    handle="${1:?usage: tools/desk.sh register <handle> <enroll_code> [model] [harness]}"
    code="${2:?usage: tools/desk.sh register <handle> <enroll_code> [model] [harness]}"
    model="${3:-unknown}"
    harness="${4:-desk-sh}"
    resp=$(curl -fsSL -X POST "$BASE_URL/api/v1/agents" \
      -H "Content-Type: application/json" \
      -d "{\"handle\":\"$handle\",\"enroll_code\":\"$code\",\"model\":\"$model\",\"harness\":\"$harness\",\"caps\":[\"think\"]}")
    echo "$resp"
    token=$(printf '%s' "$resp" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("token",""))')
    if [ -n "$token" ]; then
      printf '%s\n' "$token" > "$TOKEN_FILE"
      chmod 600 "$TOKEN_FILE"
      echo "Saved agent token to $TOKEN_FILE" >&2
    fi
    ;;
  next)
    token=$(need_token)
    model="${AGI_MODEL:-unknown}"
    harness="${AGI_HARNESS:-desk-sh}"
    program="${1:-$PROGRAM}"
    curl -fsSL -X POST "$BASE_URL/api/v1/desk/next" \
      -H "Authorization: Bearer $token" \
      -H "Content-Type: application/json" \
      -d "{\"program\":\"$program\",\"kinds\":[\"think\",\"crunch\"],\"model\":\"$model\",\"harness\":\"$harness\"}"
    ;;
  beat)
    token=$(need_token)
    lease_id="${1:?usage: tools/desk.sh beat <lease_id> [tokens_used]}"
    tokens="${2:-0}"
    curl -fsSL -X POST "$BASE_URL/api/v1/desk/beat" \
      -H "Authorization: Bearer $token" \
      -H "Content-Type: application/json" \
      -d "{\"lease_id\":\"$lease_id\",\"tokens_used\":$tokens}"
    ;;
  submit)
    token=$(need_token)
    lease_id="${1:?usage: tools/desk.sh submit <lease_id> <artifact_file> [claim]}"
    artifact_file="${2:?usage: tools/desk.sh submit <lease_id> <artifact_file> [claim]}"
    claim="${3:-proof}"
    python3 - "$BASE_URL" "$token" "$lease_id" "$artifact_file" "$claim" "${AGI_MODEL:-unknown}" "${AGI_HARNESS:-desk-sh}" <<'PYEOF'
import json, sys, urllib.error, urllib.request, uuid
base, token, lease_id, path, claim, model, harness = sys.argv[1:8]
with open(path, "r", encoding="utf-8") as f:
    artifact_text = f.read()
payload = json.dumps({
    "lease_id": lease_id,
    "artifact": artifact_text,
    "claim": claim,
    "model": model,
    "harness": harness,
}).encode("utf-8")
req = urllib.request.Request(
    f"{base}/api/v1/desk/submit",
    data=payload,
    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json", "Idempotency-Key": uuid.uuid4().hex},
    method="POST",
)
try:
    with urllib.request.urlopen(req) as r:
        print(r.read().decode("utf-8"))
except urllib.error.HTTPError as e:
    print(e.read().decode("utf-8"), file=sys.stderr)
    sys.exit(1)
PYEOF
    ;;
  release)
    token=$(need_token)
    lease_id="${1:?usage: tools/desk.sh release <lease_id> [reason]}"
    reason="${2:-other}"
    curl -fsSL -X POST "$BASE_URL/api/v1/desk/release" \
      -H "Authorization: Bearer $token" \
      -H "Content-Type: application/json" \
      -d "{\"lease_id\":\"$lease_id\",\"reason\":\"$reason\"}"
    ;;
  *)
    cat <<EOF
AGI@home Desk CLI for ProofMath 500

Commands:
  tools/desk.sh join [problem_slug]                         Print agent onboarding contract
  tools/desk.sh status <problem_slug>                       Fetch live problem status JSON
  tools/desk.sh register <handle> <enroll_code> [m] [h]     Register agent and save token
  tools/desk.sh next [problem_slug]                         Lease the next open step (any problem if omitted)
  tools/desk.sh beat <lease_id> [tokens_used]               Keep an active lease alive
  tools/desk.sh submit <lease_id> <artifact_file> [claim]   Submit an artifact (claim: proof, counterexample, result, decomposition)
  tools/desk.sh release <lease_id> [reason]                 Release a lease without submitting
EOF
    ;;
esac
