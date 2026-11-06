# Verification recipes

Use the [isolated verification environment](../../feature-map/skills/verify-omnigent/SKILL.md)
for launch, readiness checks, evidence paths and cleanup. The
[feature map](../../feature-map/README.md) lists existing user entry points.
Component tests use the fixtures described in [the test guide](../../tests/README.md).
On a managed checkout, run pytest through the owning project's isolated test runner.

- [OTLP usage ingestion](features/usage-otlp.md): HTTP signals, accounting and managed-session exclusion.
