# Parent-session automatic rotation

Use this recipe when checking context notices, automatic handover deferral, or
recovery of a recorded rotation whose runner lost its turn-end arm.

From the fork coordination checkout, run the isolated related tests against the
feature checkout (replace `<checkout>` with its absolute path):

```sh
bash infra/fork-ci-check.sh <checkout> tests/server/test_auto_rotation.py tests/server/test_auto_rotation_notices.py tests/server/test_session_handover.py tests/runner/test_pending_rotation_idle_edge.py tests/runner/test_session_handover_dispatch.py --reruns 0 -n 2 --dist loadfile
```

These component tests exercise context-round boundaries, concurrent usage writes,
notice acceptance/rejection, busy-to-idle delivery, handover deferral, startup
rediscovery, bounded retries and concurrent runner entry points. They use local
stores and simulated runtime transports; they do not prove live harness behavior.

For a live trial, launch a fresh isolated instance with the
[Verify Omnigent recipe](../../../feature-map/skills/verify-omnigent/SKILL.md).
On each rotation-capable harness (Claude Code, Codex, Pi RPC, Pi native,
Antigravity), create a top-level session with one live child. Enable automatic
rotation through its session settings or `PATCH /v1/sessions/{id}/auto-rotation`
with `{"enabled":"on","compactions":1,"remaining_pct":90}`. Compact once,
then continue work until a fresh usage reading crosses the threshold.

Observe a `[System: context]` notice, a handover tool call with `rotate=true` and
`auto=true`, and a successor that opens with the note and retains the child and
settings. If compaction or a settings change intervenes during handover, observe
`rotate:false` and the appropriate `deferred` reason, with the note retained.
For lost-arm recovery, restart only the isolated runner after recording the
request and observe recovery after two idle ticks. After two unsuccessful
re-trigger calls and two further idle ticks, the transcript should tell the
user to type `/clear`.

Keep the checkout revision, harness/version, request/result transcript, successor
and child IDs, timestamps, test output and exit code with the run's evidence.
Report each unavailable harness separately. Stop the isolated instance using its
recipe's cleanup command; the component test wrapper cleans its own temporary
HOME and runtime files.
