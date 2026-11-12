# Speed evidence

With usage enabled, authenticate as an existing test user. No live collector is
needed. POST OTLP/JSON to `/v1/me/usage/otlp/v1/metrics` containing a delta
`gen_ai.server.time_to_first_token` histogram in seconds: one closed-hour
interval, account `trial`, model `model-a`, bounds `[0.1,1]`, bucket counts
`[1,2,0]`, count `3`, and coverage `complete`. Include the timestamps as OTLP
nanoseconds and set `Metric.description` to your sample filter.

Read `/v1/me/usage/speed?range=h24&account=trial`. Expect count 3 and those exact
buckets. Resend the same body and expect count 3 again. Send count 2 and buckets
`[1,1,0]`: expect a partial-success warning with no rejected points and count 3
still stored. Add boolean point attribute `omnigent.usage.correction=true` and
resend: expect stored count 2. Another user sees no rows; a tasks-only key gets
403. Neither the usage account list nor activity totals should gain an entry.

The focused automated checks are `tests/server/routes/test_usage_speed.py`,
`tests/stores/test_usage_speed_store.py` and
`tests/db/test_migration_usage_speed.py`, run through the coordination root's
isolated `infra/fork-ci-check.sh`. Run database scope on all supported engines.
Capture only statuses and redacted responses for failures. Remove the isolated
test user when finished; this clears its speed rows.
