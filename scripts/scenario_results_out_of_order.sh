#!/usr/bin/env bash
# Brief scenario 6, results out of order: v13's summary finishes before v12's.
# mock-ai picks a call's latency when the call arrives, so v12's call is made slow and v13's fast.
# The forced version with a scripted mock: pytest -k test_6
source "$(dirname "$0")/lib.sh"
ENC="enc-order-$RUN"
require_new "$ENC"
mock_set '{"failure_rate": 0, "slow_rate": 0, "latency_min_seconds": 12, "latency_max_seconds": 12}'
T="Nurse: Hi, this is the refill line. Patient: I need to refill my metformin"

step "v12's AI call will take 12 s. Post v12 and wait until it is mid-call"
post_event "evt-order-$RUN-12" "$ENC" pat-58 MedicationRefill 12 "$T."
wait_for "$(job_in_flight "$ENC" 12)" 1 30

step "Make calls take 1 s and post v13: its summary is ready while v12's call is still running"
mock_set '{"latency_min_seconds": 1, "latency_max_seconds": 1}'
post_event "evt-order-$RUN-13" "$ENC" pat-58 MedicationRefill 13 "$T; my morning blood sugar has been high."
wait_for "$(job_status "$ENC" 13)" ready 30
get_summary "$ENC"
sql "SELECT version, status FROM summary_jobs WHERE encounter_id = '$ENC' ORDER BY version"

step "v12's result arrives last and is stored as superseded; GET is unchanged"
wait_for "$(job_status "$ENC" 12)" superseded 30
get_summary "$ENC"

step "Stored state"
sql "SELECT j.version, j.status, j.superseded_by_version, a.outcome AS attempt,
            row_number() OVER (ORDER BY a.finished_at) AS finished
       FROM summary_jobs j JOIN job_attempts a USING (job_id) WHERE j.encounter_id = '$ENC' ORDER BY a.finished_at"
