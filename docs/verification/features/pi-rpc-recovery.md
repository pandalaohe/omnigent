# Pi RPC interrupted turns

Use disposable sessions and the isolation described by the
[feature map](../../../feature-map/README.md). Real host restart authorization
is separate; [host restart](../../../feature-map/host-restart.md) describes it.

## Automated checks

Run through the checkout's established isolated pytest command. The related
modules are `tests/server/test_runner_session_init.py`,
`tests/server/test_child_session_recovery.py`,
`tests/server/integration/test_managed_startup_inputs.py`,
`tests/stores/test_subagent_retry_execution.py` and
`tests/inner/test_pi_executor.py`.

The scenarios cover shared permission-epoch initialization, original runner
errors, continued delivery after server recreation, interruption around the
runner POST, receiver deduplication, cancellation/archive/hold/new input,
delayed old failures, bootstrap usage and measured zero after compaction.
The server and Node boundaries use scripted receivers; they do not establish
Windows supervisor or real-provider behavior.

## Manual trial after deployment

1. Create a disposable Pi RPC child. Ask it to write observable progress, then
   perform a long operation whose outcome can be checked safely.
2. Restart its disposable host using the approved restart control. When the
   host returns, expect one continuation in the existing conversation without
   sending a message. Check the interrupted operation before repeating it.
3. Repeat, choosing Stop, archive or a new message before the host returns.
   The earlier continuation must not resume that task. A host-load hold delays
   delivery until the hold is released.
4. Observe the context indicator before and after reconnect. Startup must keep
   the last measurement; real compaction or a clear can reset it.

Capture server/host builds, session ID, restart time, continuation count, final
reply and any failure. Return those observations with the operation's outcome.
Clean up the disposable instance through the feature-map launch helper.

## Mid-turn input refusal

Run the related runner admission and executor injection checks through the
checkout's isolated pytest entry. An executor returning `False` is a known
refusal; a timeout or ambiguous delivery must retain the duplicate-delivery
fence. Component checks should exercise both outcomes and a buffered input
whose original admission deadline expires while the current turn runs.

After deployment, use a disposable Pi RPC session on the affected host:

1. Start a long, harmless turn and send a uniquely identifiable follow-up while
   it is active. Capture whether the executor accepts or refuses steering.
2. For a confirmed refusal, let the active turn finish after the follow-up's
   original deadline. Expect the follow-up to execute once on its continuation
   turn without resending it or reporting `Input execution admission refused`.
3. For accepted steering, expect the follow-up once in the active turn and no
   second delivery after that turn finishes. An uncertain delivery needs log
   reconciliation before any resend.

Return the host and server revisions, session/input IDs, admission and executor
outcomes, queue duration and observed execution count. Record a case without a
confirmed executor refusal as unverified for the refusal scenario. Separately
check three fresh Codex SDK children accepting their first message; a Pi
steering result does not establish the cause of a Codex first-turn failure.

## Server reconnect with a retained runner

The reconnect cases in `tests/runner/test_pi_rpc_lifecycle.py` reinitialize the
same runner session both between turns and after tool authorization. The real
Pi process must still read the marker file using the original scope. The same
module checks full-tier transitions and harness/executor replacement; the
bootstrap tests check process-identity loss and launch invalidation.

After deployment, repeat with a disposable server and Pi RPC session: restart
only the server while the runner stays alive. Expect the turn to continue after
tunnel reconnection, without an authority-transport-lost exit. Record server and
runner revisions, session ID, restart/reconnect times and the tool's outcome.
This trial needs its own server restart authorization.
