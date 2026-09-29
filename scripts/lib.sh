# Shared helpers for the scenario scripts (docs/REVIEW_GUIDE.md). Sourced, not run.
# Needs only bash, curl and docker compose on the host: no jq, no Python.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

API="http://localhost:${API_PORT:-8000}"
MOCK="http://localhost:${MOCK_AI_PORT:-8001}"
RUN="${RUN:-1}"             # RUN=2 reruns a scenario with fresh IDs without a reset

# docker compose without progress output, then one line saying what it did
compose() { docker compose --progress quiet "$@"; echo "docker compose $*: done"; }

step() { printf '\n== %s\n' "$*"; }

# Prints "<status> <body>" for one request
show() {
  local out
  out=$(curl -s -w '\n%{http_code}' "$@")
  printf '%s %s\n' "${out##*$'\n'}" "${out%$'\n'*}"
}

# post_event EVENT_ID ENCOUNTER_ID PATIENT_ID ENCOUNTER_TYPE VERSION TRANSCRIPTION
event_json() {
  printf '{"event_id":"%s","encounter_id":"%s","patient_id":"%s","encounter_type":"%s","version":%s,"payload":{"transcription":"%s"}}' "$@"
}
post_event() {
  printf 'POST v%-3s %s -> ' "$5" "$1"
  show -X POST "$API/encounters/events" -H 'content-type: application/json' -d "$(event_json "$@")"
}
# GET, condensed to the fields that change between states. VERBOSE=1 prints the whole body.
get_summary() {
  local out code body key value
  out=$(curl -s -w '\n%{http_code}' "$API/encounters/$1/summary")
  code=${out##*$'\n'}; body=${out%$'\n'*}
  if [ -n "${VERBOSE:-}" ]; then
    printf 'GET  %s -> %s %s\n' "$1" "$code" "$body"
    return
  fi
  printf 'GET  %s -> %s' "$1" "$code"
  for key in status current_version summary_version attempts error_class sla_breached; do
    value=$(grep -o "\"$key\":[^,}]*" <<<"$body" | head -1 | cut -d: -f2- | tr -d '"' || true)
    [ -n "$value" ] && printf ' %s=%s' "$key" "$value"
  done
  echo
}

# SQL as a table (blank lines dropped), or as a bare value
sql() { docker compose exec -T db psql -U outcomes -d outcomes -P footer=off -c "$1" | grep -v '^$'; }
sql_value() { docker compose exec -T db psql -U outcomes -d outcomes -Atc "$1"; }

# wait_for "SQL returning one value" EXPECTED TIMEOUT_SECONDS
wait_for() {
  local deadline=$((SECONDS + $3)) got
  while :; do
    got=$(sql_value "$1")
    [ "$got" = "$2" ] && return 0
    if [ "$SECONDS" -ge "$deadline" ]; then
      echo "Timed out after $3 s waiting for '$2' (last value: '$got')" >&2
      return 1
    fi
    sleep 0.5
  done
}

job_status() { echo "SELECT status FROM summary_jobs WHERE encounter_id = '$1' AND version = $2"; }
job_in_flight() {
  echo "SELECT count(*) FROM job_attempts a JOIN summary_jobs j USING (job_id)
         WHERE j.encounter_id = '$1' AND j.version = $2 AND a.outcome = 'in_flight'"
}

# Refuse to reuse IDs, so each scenario starts from nothing whatever ran before it
require_new() {
  if [ -n "$(sql_value "SELECT 1 FROM encounters WHERE encounter_id = '$1'")" ]; then
    echo "$1 already exists: rerun with RUN=$((RUN + 1)) $0, or reset with: docker compose down -v" >&2
    exit 1
  fi
}

# Pin the mock provider for this scenario and put its settings back on exit (even on Ctrl-C).
# Scenarios switch off random failures and slow calls so every run prints the same thing.
MOCK_SAVED=""
mock_set() {
  if [ -z "$MOCK_SAVED" ]; then
    MOCK_SAVED=$(curl -sf "$MOCK/admin/settings")
    trap mock_restore EXIT
  fi
  curl -sf -o /dev/null -X POST "$MOCK/admin/settings" -H 'content-type: application/json' -d "$1"
}
mock_restore() {
  curl -sf -o /dev/null -X POST "$MOCK/admin/settings" -H 'content-type: application/json' -d "$MOCK_SAVED" || true
}

curl -sf -o /dev/null "$API/healthz" || { echo "API not healthy at $API: run docker compose up --build -d --wait" >&2; exit 1; }
