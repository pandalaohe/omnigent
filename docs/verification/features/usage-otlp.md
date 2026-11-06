# OTLP usage ingestion

## Setup

Install the checkout's locked dependencies, including the `test` and `lint` groups.
Use an isolated server from the [verification environment](../../../feature-map/skills/verify-omnigent/SKILL.md),
with usage enabled and its own database and HOME. No vendor CLI, login or real credential is
needed for the component scenarios.

## Drive and evidence

Run `tests/server/routes/test_usage_otlp.py` with the owning project's isolated test runner.
The scenarios POST the documented fixtures through the real router and read the resulting
database rows. Keep the test output and exit status. Include
`tests/stores/test_usage_hours_store.py`, `tests/stores/test_usage_snapshot_store.py`,
`tests/inner/test_codex_config_sync.py` and `tests/runner/test_app_claude_native_launch_args.py`
when checking storage or launch changes. Run `scripts/dump_openapi.py --check` as well.

For a manual HTTP check on the isolated server, POST the `body` object from
`tests/fixtures/usage_otlp/claude.json` to `/v1/me/usage/otlp/v1/metrics` with
`Content-Type: application/json`. Expect 200 and `{}`. Repeating it must not add usage.
For the fixture's UTC hour, the account `claude-code` has 40 uncached input, 60 cache read,
10 cache creation, 20 output tokens and USD 0.125 on the list basis. Setting the sum's
`aggregationTemporality` to 2 must instead return OTLP partial success with the delta-setting hint.
Adding resource attribute `launch_mode=omni` must produce success with no usage row.

The suite also exercises protobuf, gzip, LiteLLM's cache opt-in and default modes, two-hour
retry expiry, writer conflicts, owner isolation, restart without a flush, storage rollback,
body limits and a logs body that raises if read. An account writer and its additive counters
commit together; failed writes can be retried.

## Limits and cleanup

Fixtures are documentation examples, not recordings. Gemini CLI's delta setting and exclusion
on a real vendor export require a separate check at first real use; these component tests do not
establish either fact. Stop only the isolated verification environment using its own cleanup
command and retain its evidence. No host-wide daemon or real agent configuration needs changing.
