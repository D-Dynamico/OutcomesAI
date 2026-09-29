#!/usr/bin/env bash
# Start and verify: health, schema, one encounter from POST to a ready summary, then validation.
source "$(dirname "$0")/lib.sh"
ENC="enc-verify-$RUN"
require_new "$ENC"
mock_set '{"failure_rate": 0, "slow_rate": 0}'

step "Health"
printf 'GET  /healthz -> '; show "$API/healthz"

step "Schema: five tables"
sql "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename"

step "One encounter, accepted, then summarised in the background"
post_event "evt-verify-$RUN" "$ENC" pat-33 Appointment 1 "Nurse: Good morning. How have you been feeling since your last visit? Patient: Pretty good overall, but a little more tired than usual."
get_summary "$ENC"
wait_for "$(job_status "$ENC" 1)" ready 30
get_summary "$ENC"

step "Validation happens before any database work"
printf 'POST version 0   -> '
show -X POST "$API/encounters/events" -H 'content-type: application/json' \
  -d "$(event_json "evt-verify-bad-$RUN" "$ENC" pat-33 Appointment 0 "x")"
printf 'POST 1.1 MB body -> '
head -c 1100000 /dev/zero | tr '\0' 'a' \
  | show -X POST "$API/encounters/events" -H 'content-type: application/json' --data-binary @-
