# Task board

The Task board lists tasks reported by external sources. Tasks are grouped by
source and group, with a progress icon and a link to the source's task page.
Editing happens in the source. A missing task-page link is shown as text.

## Entry points

- Settings → General → Task board → Show Task board enables the sidebar row.
- The sidebar's Task board row opens the list. On a phone, open the sidebar first.
- A direct `/task-board` link explains an inactive user switch and links to it in
  Settings. A server with `task_board.enabled: false` hides the row and settings
  group, disables task routes and explains its disabled state at the direct link.
- A source reports a JSCalendar Task or Group to
  `PUT /v1/me/tasks/sources/{source_id}`. Readers use `GET /v1/me/tasks` and
  `GET /v1/me/tasks/sources`. These routes use the server's normal login or a
  configured `tasks` API key in accounts mode.

## Checks

Use the [verification recipe](skills/verify-omnigent/SKILL.md) for an isolated
instance. On that instance, report two tasks in a Group: one in progress with
an `alternate` HTTPS link, and one cancelled without a page. Enable the display
switch, open the sidebar row and confirm both tasks appear under their source,
with distinct progress icons. Open the task's source link and confirm its URL.
Repeat through a direct link and at 390 px width. The board should fit the
viewport and preserve the saved display switch after reloading.

Related automated checks:

- `tests/server/test_task_board.py`: report/read, replay, older and conflicting
  bodies, history append, invalid-report atomicity, owner/workspace isolation.
- `tests/server/routes/test_usage_read_auth.py`: exact key scope and live owner
  authority, including denial of source deletion and unrelated routes.
- `tests/server/test_app.py::test_task_board_deployment_gate_and_info`: deployment
  gate and capability flag.
- `tests/db/test_migration_task_board.py`: additive SQLite migration round trip.
- `web/src/pages/taskBoard/`: component, pagination, safe-link, catalogue and
  literal-label tests; existing App, Settings and Sidebar tests cover mounting.
- `tests/browser_ui/test_task_board.py`: built-SPA settings → sidebar → list at
  desktop and phone widths, safe links and runtime errors. It uses synthetic
  backend responses; it does not prove live report transport or account login.

Capture the visible list and any failed request when reporting a failure. Include
the viewport, which entry point was used and whether the deployment and display
switches were enabled. Follow the recipe's cleanup after the run.
