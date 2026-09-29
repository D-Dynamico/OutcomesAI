#!/usr/bin/env bash
# Brief scenario 7, part 2, retry exhaustion, inspection and redrive (design 6.7).
# The provider fails every call but the breaker stays closed (10 failures, below its 11), so each
# job spends its own budget of 5 with backoff of about 10, 20, 40 and 80 s, then fails. About 3 minutes.
# The forced versions: pytest -k "fifth_failure or redrive_current or redrive_obsolete or fresh_budget"
source "$(dirname "$0")/lib.sh"
A="enc-exhaust-$RUN-a"
B="enc-exhaust-$RUN-b"
require_new "$A"
mock_set '{"failure_rate": 1, "slow_rate": 0, "latency_min_seconds": 0.1, "latency_max_seconds": 0.3, "timeout_hang_seconds": 0}'
T="Nurse: Hi, this is the refill line. Patient: I need to refill my metformin"

step "The provider fails every call. Post two encounters"
post_event "evt-exhaust-$RUN-a1" "$A" pat-58 MedicationRefill 1 "$T."
post_event "evt-exhaust-$RUN-b1" "$B" pat-59 MedicationRefill 1 "$T."
get_summary "$A"

step "Waiting for both jobs to spend their budget of 5 (about 2 minutes)"
wait_for "$(job_status "$A" 1)" failed 240
wait_for "$(job_status "$B" 1)" failed 240
get_summary "$A"

step "Inspect: the attempt history, one row per paid call, no patient content"
sql "SELECT a.attempt_no, a.redrive_generation, a.outcome, a.error_class,
            round(extract(epoch FROM a.started_at - lag(a.finished_at) OVER (ORDER BY a.attempt_no))) AS backoff_s
       FROM job_attempts a JOIN summary_jobs j USING (job_id)
      WHERE j.encounter_id = '$A' ORDER BY a.attempt_no"

step "Fix the provider. Meanwhile $B gets a newer version, which succeeds"
mock_set '{"failure_rate": 0, "latency_min_seconds": 1, "latency_max_seconds": 2}'
post_event "evt-exhaust-$RUN-b2" "$B" pat-59 MedicationRefill 2 "$T; my blood sugar has been high."
wait_for "$(job_status "$B" 2)" ready 30

step "Redrive both failed jobs: $A's is still current, $B's is not"
JA=$(sql_value "SELECT job_id FROM summary_jobs WHERE encounter_id = '$A' AND version = 1")
JB=$(sql_value "SELECT job_id FROM summary_jobs WHERE encounter_id = '$B' AND version = 1")
printf 'POST /admin/jobs/%s/redrive (%s v1) -> ' "$JA" "$A"; show -X POST "$API/admin/jobs/$JA/redrive"
printf 'POST /admin/jobs/%s/redrive (%s v1) -> ' "$JB" "$B"; show -X POST "$API/admin/jobs/$JB/redrive"
wait_for "$(job_status "$A" 1)" ready 30
get_summary "$A"

step "A job that is not failed cannot be redriven"
printf 'POST /admin/jobs/%s/redrive (%s v1) -> ' "$JA" "$A"; show -X POST "$API/admin/jobs/$JA/redrive"

step "Stored state: the redrive got a fresh budget (generation 1); $B v1 was never paid for again"
sql "SELECT j.encounter_id, j.version, j.status, j.superseded_by_version, a.redrive_generation,
            count(a.*) AS attempts, string_agg(DISTINCT a.outcome::text, ', ') AS outcomes
       FROM summary_jobs j JOIN job_attempts a USING (job_id)
      WHERE j.encounter_id IN ('$A', '$B') GROUP BY 1, 2, 3, 4, 5 ORDER BY 1, 2, 5"
