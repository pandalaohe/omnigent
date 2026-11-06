# Verification

The [feature map](../../feature-map/README.md) owns launch, isolation, interaction,
evidence and cleanup recipes. Start with its Verify Omnigent skill for a running
app; component checks use their own isolated test environment.

- [Model sources](features/model-sources.md): model list card and offline checks.

Record the checkout revision, command, result and evidence location for each run.
An HTML preview with fake data proves the shown component interactions, rather
than a deployed host or a live provider. Clean up only the test processes and
temporary directories the run created.
