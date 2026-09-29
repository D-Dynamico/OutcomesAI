#!/usr/bin/env bash
# Brief scenario 8, inconsistent identity fields: a later event changes the patient or the type.
# The forced race between two first events for a brand-new encounter: pytest -k test_3
source "$(dirname "$0")/lib.sh"
ENC="enc-identity-$RUN"
require_new "$ENC"
mock_set '{"failure_rate": 0, "slow_rate": 0}'
T="Nurse: Good morning. How have you been feeling since your last visit?"

step "v1 for patient pat-33, then v2 with a different patient, then with a different type"
post_event "evt-identity-$RUN-1" "$ENC" pat-33 Appointment 1 "$T"
post_event "evt-identity-$RUN-2" "$ENC" pat-99 Appointment 2 "$T Patient: Tired."
post_event "evt-identity-$RUN-2" "$ENC" pat-33 MedicationRefill 2 "$T Patient: Tired."

step "The same v2 with the stored identity is accepted"
post_event "evt-identity-$RUN-2" "$ENC" pat-33 Appointment 2 "$T Patient: Tired."

step "Stored state: identity unchanged; the rejected events were never stored"
sql "SELECT encounter_id, patient_id, encounter_type, current_version FROM encounters WHERE encounter_id = '$ENC'"
sql "SELECT event_id, version FROM encounter_events WHERE encounter_id = '$ENC' ORDER BY version"

step "The one log line allowed to carry patient IDs, for diagnosing the partner's mapping bug"
docker compose logs --no-log-prefix api | grep '"identity_conflict"' | grep "\"$ENC\""
