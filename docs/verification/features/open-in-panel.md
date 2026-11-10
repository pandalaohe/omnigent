# Agent requests to open a file

Use the [verification entry](../README.md) for isolation and cleanup. Files and
the workspace browser are not yet mapped in the feature map; the checks below
cover artifact request routing and tool reporting.

## Component and runner checks

Use Node 24 for web vitest. From `web/`, run the AppShell, GridSessionPane and
artifactOpenBus test files with `pnpm exec vitest run`. These checks distinguish
an active workspace, a background session, an unmounted consumer, grid selection
and guarded navigation. Runner coverage lives in
`tests/runner/test_open_in_panel_dispatch.py`; use the project's isolated pytest
wrapper. `tests/server/routes/test_artifacts.py` covers the artifact route and
the file-serving refusals separately.

## Manual acceptance

In an isolated instance started through Verify Omnigent, create two sessions
with readable HTML and Markdown files. Collapse the workspace, call
`open_in_panel` from the selected session and check that its file appears.
Switch to the other session while the first session emits another request;
return to the first and check that the latest received file appears. Repeat in
a grid with the issuing pane unselected, then select it. With unsaved edits,
decline navigation and check that the editor is preserved.

Check the tool result: zero subscribers reports `not_delivered` and asks for a
retry; positive subscribers reports `sent_unconfirmed`. Neither confirms that
a human saw the file. Client-side deferral lasts for this page lifetime only;
requests lost before reaching the client are not replayed by the server.
Multiple browsers handle their own received request independently. A successful
event publication also does not prove file existence, read permission or preview
loading. Stop the isolated instance and owned browser when done.
