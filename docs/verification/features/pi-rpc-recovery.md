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
