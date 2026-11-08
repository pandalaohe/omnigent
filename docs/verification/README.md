# Verification

The [feature map](../../feature-map/README.md) owns launch, isolation, interaction,
evidence and cleanup recipes. Start with its Verify Omnigent skill for a running
app; component checks use their own isolated test environment.

- [Model sources](features/model-sources.md): model list card and offline checks.
- [Starting prompts](features/startup-prompts.md): session text, owner versions,
  restore/Undo, reconnect and agents whose prompts are not sent.
- [Pi RPC interrupted turns](features/pi-rpc-recovery.md): reconnect, continuation
  receipts, user controls, permission initialization and context usage.
- [OTLP usage ingestion](features/usage-otlp.md): HTTP signals, accounting and managed-session exclusion.
- [Automatic rotation](features/auto-rotation.md): context notices, handover deferral,
  durable request recovery and runner concurrency.
- [Host upgrades](features/host-upgrade.md): legacy channel pins, Windows file users,
  bounded retries and failed-install recovery.

Record the checkout revision, command, result and evidence location for each run.
An HTML preview with fake data proves the shown component interactions, rather
than a deployed host or a live provider. Clean up only the test processes and
temporary directories the run created.
