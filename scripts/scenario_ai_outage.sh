#!/usr/bin/env bash
# Brief scenario 7, part 1, AI outage (design 6.7): 40 jobs, 3 worker containers, the provider
# failing every call. The shared breaker trips after 11 failures; while it is open no job is
# claimed (so no retry budget is spent) and one synthetic probe goes out per 30 s cooldown.
# Production timings, so it takes about 3 minutes. It resets mock-ai's call counters.
# The compressed version, 100 jobs and 4 workers: pytest -k test_5
source "$(dirname "$0")/lib.sh"
PREFIX="enc-outage-$RUN"
require_new "$PREFIX-1"
mock_set '{"failure_rate": 0, "slow_rate": 0}'

snapshot() {
  printf 't=%3ss  breaker: %-9s probes_sent=%s  jobs: %-33s mock-ai: %s\n' "$((SECONDS - START))" \
    "$(sql_value "SELECT state FROM circuit_breaker")" \
    "$(sql_value "SELECT probes_sent FROM circuit_breaker")" \
    "$(sql_value "SELECT string_agg(status || '=' || n, ' ' ORDER BY status) FROM (
                    SELECT status::text, count(*) AS n FROM summary_jobs
                     WHERE encounter_id LIKE '$PREFIX-%' GROUP BY 1) s")" \
    "$(curl -s "$MOCK/admin/stats")"
}

step "Three worker containers: 12 worker loops"
compose up -d --wait --scale worker=3 worker

step "Reset mock-ai's call counters and switch it into an outage"
curl -s -X POST "$MOCK/admin/reset"; echo
mock_set '{"outage": true}'
START=$SECONDS

step "Post 40 encounters"
for i in $(seq 1 40); do
  curl -s -o /dev/null -w '%{http_code}\n' -X POST "$API/encounters/events" -H 'content-type: application/json' \
    -d "$(event_json "evt-outage-$RUN-$i" "$PREFIX-$i" "pat-$i" TelephoneTriage 1 "Nurse: Triage line, call $i.")"
done | sort | uniq -c

step "Outage: sample every 30 s. Real calls stop at the trip; probes go up by one per cooldown"
for t in 5 35 65 95; do
  while [ $((SECONDS - START)) -lt "$t" ]; do sleep 0.5; done
  snapshot
done

step "What a client and the metrics show during the outage"
get_summary "$PREFIX-1"
curl -s "$API/metrics" | grep -E '^(breaker_state|breaker_probes_sent_total|summary_sla_breaching_jobs|summary_jobs\{status="(queued|failed)"\})'

step "End the outage. The next probe closes the breaker, then the queue drains"
mock_set '{"outage": false}'
until [ "$(sql_value "SELECT count(*) FROM summary_jobs WHERE encounter_id LIKE '$PREFIX-%' AND status = 'ready'")" = 40 ]; do
  snapshot
  [ $((SECONDS - START)) -lt 360 ] || { echo "Timed out waiting for 40 ready" >&2; exit 1; }
  sleep 10
done
snapshot
get_summary "$PREFIX-1"

step "Stored state: every job ready, none failed; real calls per outcome"
sql "SELECT status, count(*) FROM summary_jobs WHERE encounter_id LIKE '$PREFIX-%' GROUP BY status"
sql "SELECT a.outcome, a.error_class, count(*) FROM job_attempts a JOIN summary_jobs j USING (job_id)
      WHERE j.encounter_id LIKE '$PREFIX-%' GROUP BY 1, 2 ORDER BY 1"

step "Back to one worker container"
compose up -d --wait --scale worker=1 worker
