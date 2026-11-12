# Task source snapshots

Use an isolated development server built from this checkout. Never point a trial at an
unrelated user's source. The protocol and hash inputs are defined in
[Task board sources](../../task-board-sources.md#display-projections-and-snapshot-updates-report-schema-revision-2),
with executable [wire vectors](../../schemas/task-board-snapshot-vectors.json).

## Automated consumer checks

Run the related Python server and migration files through the project's isolated pytest
launcher. Exercise the same scope on PostgreSQL 16, MySQL 8.0, and CockroachDB 23.2.28 before
landing a migration. Those results are distinct from SQLite and schema validation.
Run the taskBoard Vitest scope under Node 24, TypeScript, and touched-file lint/format checks.
Record checkout/diff identity, commands, exits, backend/version and evidence paths. A missing
engine or URI is unverified, not a skipped-pass database lane.

## Trial

1. Use the wire vectors with the existing authenticated PUT route. Send begin and read the
   returned sourceGeneration; substitute that value into append and publish. While staging,
   task/item/history/count/display reads must return 409 snapshot_updating. An ordinary legacy
   PUT to that source must return 409 snapshot_required.
2. Send append, retry the identical append, then publish twice. Read with the snapshot id and
   source generation from the receipt. Both publish receipts must agree, and all pages must
   contain the published generation. Change the body at the same chunk index to observe 409
   chunk_conflict. Never use a destructive source-delete trial on shared data.
3. In Task board, open a supplied task. Source details appears only for populated badges,
   rows or links; strings such as `<b>literal</b>` remain text. Links open only after a click,
   in a separate tab. A source upload never sends a session prompt or changes a native status.
4. Keep the page open during another begin: the previous complete reading stays visible with
   an updating indication. A cold page waits; it must not display partially replaced tasks.
   Publish the remaining chunks and verify the whole new reading arrives together.

Capture the response status/code, source/snapshot ids (no credential), page action and expected
versus observed result. Stop only the isolated instance this trial started; preserve evidence
and remove its own temporary data through the fixture's cleanup route.

The standalone acceptance report proves its component/adapter scenarios, not deployment,
three-backend transactions, pointer behavior, or visual parity. Additional browser runs require
the task's explicit browser authorization.
