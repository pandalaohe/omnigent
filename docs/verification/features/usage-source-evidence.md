# Source evidence

Prerequisite: a configured development server with usage enabled and an existing
authenticated account. Use isolated data for automated checks. No vendor or
gateway connection is required.

Related checks (from the coordination root, with the checkout's own locked env):

```sh
bash infra/fork-ci-check.sh <checkout> tests/server/routes/test_usage_evidence.py tests/server/routes/test_usage_read_auth.py tests/stores/test_usage_evidence_store.py tests/db/test_migration_usage_source_evidence.py -m 'not databricks'
```

Run the store and migration files with `--test-db-backend` and the caller's
temporary `OMNIGENT_TEST_DB_URI` for each supported database. Keep credentials
out of logs. Each temporary database belongs to this run and is removed on exit.

Manual API trial: authenticate as a test user, send a current RFC 3339 CloudEvent
with source `/collector`, id `trial`, type `usage.status`, then resend it. Expect
accepted 1 followed by duplicates 1; GET events returns one unchanged event.
PUT a source-wide `ok` snapshot with an expired `fresh_until`; GET sources shows
`account: null, current: false`. A different user sees neither row. An account
filter excludes source-wide rows. Capture status and redacted response for any
failure; omit authorization headers. Remove the isolated test user afterward.
