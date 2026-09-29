#!/usr/bin/env bash
# Brief scenario 5, newer version during processing: v13 arrives while v12 is being summarised.
# v12's call completes and its result is kept, but as superseded: it is never shown.
# The forced version with a scripted mock: pytest -k test_in_flight_result_for_obsolete_version
source "$(dirname "$0")/lib.sh"
ENC="enc-newer-$RUN"
require_new "$ENC"
mock_set '{"failure_rate": 0, "slow_rate": 0, "latency_min_seconds": 4, "latency_max_seconds": 4}'
T="Nurse: Triage line, what is going on today? Patient: I have had a fever since last night"

step "v12's AI call takes 4 s. Post v12 and wait until it is mid-call"
post_event "evt-newer-$RUN-12" "$ENC" pat-91 TelephoneTriage 12 "$T."
wait_for "$(job_in_flight "$ENC" 12)" 1 30

step "v13 arrives while v12 is being summarised; its call will take 7 s"
mock_set '{"latency_min_seconds": 7, "latency_max_seconds": 7}'
post_event "evt-newer-$RUN-13" "$ENC" pat-91 TelephoneTriage 13 "$T and now my throat hurts."
get_summary "$ENC"

step "v12's call finishes first: its result is stored as superseded, and GET still waits for v13"
wait_for "$(job_status "$ENC" 12)" superseded 30
get_summary "$ENC"

step "v13's call finishes"
wait_for "$(job_status "$ENC" 13)" ready 30
get_summary "$ENC"

step "Stored state: v12 paid for once and kept for history, marked superseded by 13"
sql "SELECT j.version, j.status, j.superseded_by_version, j.summary IS NOT NULL AS has_summary, a.outcome AS attempt
       FROM summary_jobs j JOIN job_attempts a USING (job_id) WHERE j.encounter_id = '$ENC' ORDER BY j.version"
