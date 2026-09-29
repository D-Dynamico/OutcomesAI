#!/usr/bin/env bash
# Brief scenario 1, concurrent duplicate: the same event arrives several times at once.
# The forced interleaving, 200 iterations with a row-lock hold: pytest -k test_1
source "$(dirname "$0")/lib.sh"
ENC="enc-dup-$RUN"
require_new "$ENC"
mock_set '{"failure_rate": 0, "slow_rate": 0}'

# Sends the same body N times in parallel and counts the distinct responses
fire() {
  local tmp; tmp=$(mktemp -d)
  for i in $(seq 1 "$1"); do
    show -X POST "$API/encounters/events" -H 'content-type: application/json' -d "$2" > "$tmp/$i" &
  done
  wait
  sort "$tmp"/* | uniq -c
  rm -rf "$tmp"
}

step "10 identical requests at once, brand-new encounter (v1)"
fire 10 "$(event_json "evt-dup-$RUN-v1" "$ENC" pat-58 MedicationRefill 1 "Nurse: Hi, this is the refill line. How can I help? Patient: I need to refill my metformin.")"

step "10 identical requests at once, existing encounter (v2)"
fire 10 "$(event_json "evt-dup-$RUN-v2" "$ENC" pat-58 MedicationRefill 2 "Nurse: Hi, this is the refill line. How can I help? Patient: I need to refill my metformin. Nurse: Any side effects?")"

step "Stored state: one event and one job per version"
sql "SELECT ev.version, count(DISTINCT ev.event_id) AS events, count(DISTINCT j.job_id) AS jobs
       FROM encounter_events ev JOIN summary_jobs j USING (encounter_id, version)
      WHERE ev.encounter_id = '$ENC' GROUP BY ev.version ORDER BY ev.version"
sql "SELECT encounter_id, patient_id, current_version FROM encounters WHERE encounter_id = '$ENC'"
