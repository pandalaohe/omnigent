# Task board

The Task board opens with native sessions, their child sessions and plan steps.
Routes shows reported tasks as ordered rails or a time map, grouped by source
and group. A task opens its status, next step, waiting reason, related tasks,
history and linked sessions. Editing happens in the source; a missing task-page
link is shown as text. Waiting on me combines source tasks and native prompts.

## Entry points

- Settings → General → Task board → Show Task board enables the sidebar row.
- The sidebar's Task board row opens the board. On a phone, open the sidebar first.
- A direct `/task-board` link explains an inactive user switch and links to it in
  Settings. A server with `task_board.enabled: false` hides the row and settings
  group, disables task routes and explains its disabled state at the direct link.
- A source reports a JSCalendar Task or Group to
  `PUT /v1/me/tasks/sources/{source_id}`. Readers use `GET /v1/me/tasks` and
  `GET /v1/me/tasks/sources`. These routes use the server's normal login or a
  configured `tasks` API key in accounts mode.
- Readers open a task through `GET /v1/me/tasks/item`, browse events through
  `/history`, and get daily closing totals through `/closed-counts`, all under
  `/v1/me/tasks`. A logged-in owner can delete a source with
  `DELETE /v1/me/tasks/sources/{source_id}`; a feeder key cannot delete it.

## Checks

Use the [verification recipe](skills/verify-omnigent/SKILL.md) for an isolated
instance. On that instance, report two tasks in a Group: one in progress with
an `alternate` HTTPS link, and one cancelled without a page. Enable the display
switch and open the sidebar row. Confirm Sessions shows a parent and child,
their real session links, available plan steps and recently archived sessions.
Open Routes and confirm both tasks appear under their source with distinct
progress icons. Open a task and follow its source link; a cancelled task without
a link explains that the source gives no page.
Repeat through a direct link and at 390 px width. The board should fit the
viewport and preserve the saved display switch after reloading.

For the report and read API, use an isolated instance and a disposable source:

1. Report a Group with two open Tasks and one completed Task without
   `progressUpdated`. Read the completed item and confirm `omnigent.ai:closedSeen`
   is present, then find it in the daily closing totals using its observed time.
2. Report just one open Task with `omnigent.ai:complete: "open"` on the Group.
   The omitted open Task disappears from a normal read and appears with
   `omnigent.ai:gone` in a change read using `changed_after`. The completed Task
   remains visible. Resend the omitted Task and confirm the reply reports it as
   returned.
3. Read a source with a small page limit, following every cursor. After finishing
   the change-read pages, reuse the first page's `omnigent.ai:changedThrough`
   minus 60 seconds for the next read. Make a report between pages and confirm
   the next change read reconciles the changed tasks and gone markers.
4. Append a history entry while resending an older Task body. Confirm the reply
   holds the body and adds the history, then find that entry in the item and in
   `/history`. Read `/closed-counts` with a fixed UTC offset to check day edges.
5. Delete the disposable source using login authentication. Confirm it, its
   items and its history disappear. Recreate it and confirm its `created` value
   changes; readers use that value to discard the source's previous cache.

Report failed API checks with the route, HTTP status and response code, omitting
credentials. Report pagination failures with page sizes and the first watermark.

In Routes, switch to By time. Pan with the wheel, zoom with Ctrl or Command plus
wheel, and reset to the initial window. Zooming keeps the right edge fixed and
stops at twelve hours. At wider ranges, stations become groups and density bars.
Use Focus to select groups, change modes and confirm the choice persists; Reset
shows all groups again. Search supports repeated Enter picks, arrows, select all,
invert, and Escape to clear before closing. At 390 px, By order uses task rails
with explicit relation links.

Report a task waiting on the user and open a native prompt. Waiting on me shows
both; its session link opens the native session. Inspect network requests while
using the board: task and native reads use GET, and its dedicated updates socket
sends only watch frames. A later source report, gone task, deleted/recreated source
and a change spanning multiple pages should converge on the next visible refresh.

Related automated checks:

- `tests/server/test_task_board.py`: report/read, replay, older and conflicting
  bodies, completeness/gone, change-read pagination under concurrent reports,
  item/history/counts/delete, report limits, issues and owner/workspace isolation.
- `tests/server/routes/test_usage_read_auth.py`: exact key scope and live owner
  authority, including denial of source deletion and unrelated routes.
- `tests/server/test_app.py::test_task_board_deployment_gate_and_info`: deployment
  gate and capability flag.
- `tests/server/test_app.py::test_task_board_raised_limits_reach_the_live_report_router`:
  configured body, entry and source limits through the real app router.
- `tests/db/test_migration_task_board.py`: additive SQLite migration round trip.
- `web/src/pages/taskBoard/`: native session trees, wait joins, incremental page
  reads, source recreation, safe links, route geometry, search keys, catalogue
  and literal-label tests; existing App, Settings and Sidebar tests cover mounting.
- `tests/browser_ui/test_task_board.py`: built-SPA settings → sidebar → Sessions
  and Routes at desktop dark/light and phone widths, details, safe links,
  watch-only frames and GET-only interactions. It uses synthetic
  backend responses; it does not prove live report transport or account login.

Capture the visible list and any failed request when reporting a failure. Include
the viewport, which entry point was used and whether the deployment and display
switches were enabled. Follow the recipe's cleanup after the run.
