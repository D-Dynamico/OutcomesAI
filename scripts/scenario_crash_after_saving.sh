#!/usr/bin/env bash
# Brief scenario 3, crash after saving: the update is saved, then the service dies before a
# worker has the work. The job row commits in the same transaction as the update, so there is
# no separate hand-off to lose. The forced version, killing the API process just before and
# just after COMMIT: pytest -k test_4
source "$(dirname "$0")/lib.sh"
ENC="enc-crash-$RUN"
require_new "$ENC"
mock_set '{"failure_rate": 0, "slow_rate": 0}'
BODY=("evt-crash-$RUN" "$ENC" pat-77 TelephoneTriage 1 "Nurse: Thanks for calling, how can I help today? Patient: I have a rash on my arm.")

step "Stop the workers, so nothing can pick the job up"
compose stop worker

step "Accept an update, then kill the API with SIGKILL"
post_event "${BODY[@]}"
compose kill api

step "Stored state with the API and workers down: the job was saved with the update"
sql "SELECT e.current_version, j.version, j.status, j.attempts
       FROM encounters e JOIN summary_jobs j USING (encounter_id) WHERE e.encounter_id = '$ENC'"

step "Restart the API and workers; the partner, which may not have seen the 201, retries"
compose up -d --wait api worker
post_event "${BODY[@]}"
wait_for "$(job_status "$ENC" 1)" ready 30
get_summary "$ENC"

step "Stored state: one job, summarised once"
sql "SELECT j.version, j.status, count(a.*) AS attempts
       FROM summary_jobs j JOIN job_attempts a USING (job_id)
      WHERE j.encounter_id = '$ENC' GROUP BY j.job_id"
