#!/usr/bin/env bash
# Brief scenario 2, missing or late version: v12 arrives while v10 is stored; v11 arrives later.
# The forced race, both orders, GET sampled throughout: pytest -k test_2
source "$(dirname "$0")/lib.sh"
ENC="enc-late-$RUN"
require_new "$ENC"
mock_set '{"failure_rate": 0, "slow_rate": 0}'
T="Nurse: Triage line, what is going on today? Patient: I have had a fever since last night"

step "v10, then v12 (v11 skipped), then the late v11, then a retry of v10"
post_event "evt-late-$RUN-10" "$ENC" pat-91 TelephoneTriage 10 "$T."
post_event "evt-late-$RUN-12" "$ENC" pat-91 TelephoneTriage 12 "$T and now my throat hurts."
post_event "evt-late-$RUN-11" "$ENC" pat-91 TelephoneTriage 11 "$T and it is getting worse."
post_event "evt-late-$RUN-10" "$ENC" pat-91 TelephoneTriage 10 "$T."

step "The client only ever sees v12: processing, then ready"
get_summary "$ENC"
wait_for "$(job_status "$ENC" 12)" ready 30
wait_for "$(job_status "$ENC" 10)" superseded 30
get_summary "$ENC"

step "Stored state: current_version 12, v11 never stored, v10's job superseded"
sql "SELECT e.current_version, j.version, j.status, j.superseded_by_version
       FROM encounters e JOIN summary_jobs j USING (encounter_id) WHERE e.encounter_id = '$ENC' ORDER BY j.version"
