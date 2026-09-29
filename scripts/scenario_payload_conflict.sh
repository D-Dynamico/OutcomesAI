#!/usr/bin/env bash
# Payload conflict, the brief's other invariant: a version is re-sent with a different transcript.
# More cases, with exact bodies: pytest -k payload_conflict
source "$(dirname "$0")/lib.sh"
ENC="enc-payload-$RUN"
require_new "$ENC"
mock_set '{"failure_rate": 0, "slow_rate": 0}'
A="Nurse: Any difficulty breathing or swallowing? Patient: Swallowing is painful, but breathing is fine."
B="Nurse: Any difficulty breathing or swallowing? Patient: No."

step "v1 accepted"
post_event "evt-payload-$RUN-1" "$ENC" pat-91 TelephoneTriage 1 "$A"

step "Same event_id, different transcript"
post_event "evt-payload-$RUN-1" "$ENC" pat-91 TelephoneTriage 1 "$B"

step "New event_id for the same version, different transcript"
post_event "evt-payload-$RUN-1b" "$ENC" pat-91 TelephoneTriage 1 "$B"

step "New event_id for the same version, same transcript: a source defect, treated as a duplicate"
post_event "evt-payload-$RUN-1c" "$ENC" pat-91 TelephoneTriage 1 "$A"

step "Stored state: one event with the first transcript's hash, one job"
sql "SELECT event_id, version, payload_hash = sha256(convert_to('$A', 'UTF8')) AS hash_of_first_transcript
       FROM encounter_events WHERE encounter_id = '$ENC'"
sql "SELECT version, count(*) AS jobs FROM summary_jobs WHERE encounter_id = '$ENC' GROUP BY version"
