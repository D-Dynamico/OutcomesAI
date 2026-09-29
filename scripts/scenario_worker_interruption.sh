#!/usr/bin/env bash
# Brief scenario 4, worker interruption: a worker dies mid-call. Its 60 s lease runs out, a worker
# reclaims the job, and the dead attempt is closed as lease_expired. The forced versions, including
# a stalled worker whose late result is discarded: pytest -k "test_7 or reclaim"
source "$(dirname "$0")/lib.sh"
ENC="enc-interrupt-$RUN"
require_new "$ENC"
mock_set '{"failure_rate": 0, "slow_rate": 0, "latency_min_seconds": 20, "latency_max_seconds": 20}'

step "AI calls take 20 s. Post, and wait until a worker is mid-call"
post_event "evt-interrupt-$RUN" "$ENC" pat-33 Appointment 1 "Nurse: Blood pressure is 122 over 78. Are you still taking lisinopril daily? Patient: Yes."
wait_for "$(job_in_flight "$ENC" 1)" 1 30
sql "SELECT a.attempt_no, a.outcome, j.status, j.lease_expires_at > now() AS lease_live
       FROM job_attempts a JOIN summary_jobs j USING (job_id) WHERE j.encounter_id = '$ENC'"

step "Kill the workers with SIGKILL (no graceful drain), make the AI fast again, restart them"
compose kill worker
mock_set '{"latency_min_seconds": 1, "latency_max_seconds": 2}'
compose up -d worker
get_summary "$ENC"

step "Waiting for the lease to expire and a worker to reclaim the job (about a minute)"
wait_for "$(job_status "$ENC" 1)" ready 120
get_summary "$ENC"

step "Stored state: the dead attempt closed as lease_expired, the reclaim succeeded"
sql "SELECT a.attempt_no, a.outcome, a.error_class
       FROM job_attempts a JOIN summary_jobs j USING (job_id)
      WHERE j.encounter_id = '$ENC' ORDER BY a.attempt_no"
