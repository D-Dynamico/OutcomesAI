#!/usr/bin/env bash
# Looking inside: posts a transcript with a unique marker, waits for its summary, then searches
# every service's logs for the transcript, the summary and the patient ID. Expect 0 of each.
# The automated version: pytest -k "patient_content"
source "$(dirname "$0")/lib.sh"
ENC="enc-privacy-$RUN"
PATIENT="pat-privacy-$RUN"
MARKER="Zebra$RUN-Marker-$(date +%s)"
require_new "$ENC"
mock_set '{"failure_rate": 0, "slow_rate": 0}'

step "Post a transcript containing a unique marker, wait for its summary"
post_event "evt-privacy-$RUN" "$ENC" "$PATIENT" Appointment 1 "Nurse: How are you? Patient: $MARKER."
wait_for "$(job_status "$ENC" 1)" ready 30
SUMMARY=$(sql_value "SELECT summary FROM summary_jobs WHERE encounter_id = '$ENC'")

step "Search every service's logs and the API's metrics"
LOGS=$(docker compose logs --no-log-prefix 2>&1)
printf 'log lines about %-18s %s\n' "$ENC:" "$(grep -c -- "$ENC" <<<"$LOGS" || true)"
printf 'log lines containing the transcript: %s\n' "$(grep -c -- "$MARKER" <<<"$LOGS" || true)"
printf 'log lines containing the summary:    %s\n' "$(grep -cF -- "$SUMMARY" <<<"$LOGS" || true)"
printf 'log lines containing the patient ID: %s\n' "$(grep -c -- "$PATIENT" <<<"$LOGS" || true)"
printf 'metrics lines containing any of them: %s\n' \
  "$(curl -s "$API/metrics" | grep -cE -- "$MARKER|$PATIENT|$ENC" || true)"

step "What the logs do say about this encounter, in time order"
grep -- "$ENC" <<<"$LOGS" | sort
