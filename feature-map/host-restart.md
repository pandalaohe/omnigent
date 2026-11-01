# Host restart

The owner of an online, supervised host can preview a restart, then restart that host and read its progress. A restart ends its runners and terminals. Running turns get a bounded drain period; the initiating session gets a short grace period and a result message after the host reconnects or the reconnect deadline expires.

## Sub-features

- `restart-preview`: read supervisor, running sessions, runner count and whether the requester runs on this host; previews expire after 120 seconds.
- `restart-owner`: host ownership, requester ownership and a fresh preview are required.
- `restart-progress`: draining, stopping, waiting for reconnect, done or failed; long draining does not spend the reconnect budget.
- `restart-notice`: retry the result message until the initiating session accepts it.
- `restart-supervisor`: refuse hosts without a known supervisor; a clean teardown exits 75 for the supervisor to relaunch.

## How to get to it (user POV)

Authenticated API: POST `/v1/hosts/{host_id}/restart` with `dry_run: true`. To restart, repeat with `dry_run: false` and that `preview_id`. Include `requested_by_session_id` when the result should wake a session you own. Read GET `/v1/hosts/{host_id}/restart/{request_id}` for progress.

## Driving it with the repro environment

Preconditions: use a disposable server and fake host tunnel. The ordinary isolated runner is not a supervised host. Real service restarts require separate operation approval.

- `restart-preview`, `restart-owner`, `restart-progress`: `tests/server/integration/test_hosts_api.py::test_restart_round_trip_over_fake_tunnel` and `::test_restart_owner_offline_supervisor_and_requester_guards` exercise the real host routes and fake tunnel. `tests/server/test_host_restart.py` covers preview expiry, option binding, separate deadlines and notice retry.
- `restart-supervisor`: `tests/host/test_restart.py`, `tests/host/test_service.py`, and `tests/host/test_connect.py::test_host_restart_exits_75_after_run_teardown` use fake supervisors and teardown boundaries. `::test_restart_ack_failure_releases_admission` checks recovery after an acknowledgement cannot be delivered.
- Complete running-session inventory: `tests/server/routes/test_side_chat_runner_recovery.py::test_restart_inventory_reads_running_side_chat_through_sessions_api` exercises the actual sessions API with an idle source and a running side chat sharing its runner.
- After an approved deployment, a human trial may preview a supervised disposable host, confirm its session list, restart it and poll the status. Expect a newer host start time and `done`, then a result message in the requester session. Capture the preview, request id, final status and message; report supervisor and host version. This fake-tunnel suite does not prove the operating system relaunch or a real runner wake.

## Gotchas

- A preview does not drain, stop or reserve the host.
- A preview is bound to its owner, connection, requester, force setting and drain timeout. Changing them requires another preview.
- An offline host cannot be started through this API.
- Reconnecting the same host process does not complete a restart; its start time must advance.
- Windows support detects the existing scheduled-task restart loop. Its exit-75 relaunch still needs a real platform trial.
