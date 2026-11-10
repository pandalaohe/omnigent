# Task board sources

Native Omnigent work — sessions, child sessions, plans, scheduled tasks, system findings, and quota readings — appears on the Task board automatically and needs no feeder.

External task sources (issue trackers, project boards, CI pipelines, or custom scripts) push standard IETF [RFC 8984](https://www.rfc-editor.org/rfc/rfc8984) JSCalendar `Task` or `Group` objects over HTTP. Every vendor extension property uses the `omnigent.ai:` prefix (RFC 8984 §3.3) and is marked **Omnigent-defined** in the tables below. Offline reports can be validated against [`docs/schemas/task-board-report.schema.json`](schemas/task-board-report.schema.json).

---

## Contract summary

The Task board is **read-only** with respect to external sources: Omnigent never writes back to an issue tracker or board file. When a reported task includes an `http:` or `https:` link with `rel: "alternate"` (`links.page`), the detail drawer renders an **Open source** link; when no safe link is present, the drawer states that the source provides no page.

| Route | Purpose |
|---|---|
| `PUT /v1/me/tasks/sources/{source_id}` | Push one `Task` or a `Group` of `Task` objects for `{source_id}` (`[A-Za-z0-9][A-Za-z0-9_-]{0,63}`). Accepts `Content-Type: application/json` or `application/jscalendar+json` (default limit 8 MiB, up to 10,000 tasks per report and 64 sources per owner). |
| `GET /v1/me/tasks` | List or incrementally sync tasks (`state=open\|closed\|all`, `source`, `group`, `closed_after`, `closed_before`, `changed_after`, `include_gone=0\|1`, `include_history=0\|1`, `limit`, `cursor`). |
| `GET /v1/me/tasks/item?source={source_id}&uid={uid}` | Fetch a single stored task with its history events (`omnigent.ai:history`) and unresolved relation list (`omnigent.ai:unresolved`). |
| `GET /v1/me/tasks/history` | Read the append-only event stream across tasks (`source`, `after`, `before`, `limit`, `cursor`). |
| `GET /v1/me/tasks/closed-counts` | Daily closed-task counts by source (`from`, `to`, `offset` minutes `-1439..1439`, `source`). |
| `GET /v1/me/tasks/sources` | List the owner's task sources with `id`, `name`, `source`, `created`, `last_write`, `counts` (`open`, `closed`, `gone`), and the latest 20 validation `issues`. |
| `DELETE /v1/me/tasks/sources/{source_id}` | Atomically delete a source, its tasks, and its history (`204 No Content`; login session required). |

Deployment operators can disable the routes and UI by setting `task_board.enabled: false` in `config.yaml`, or tune caps under `task_board.limits` (`max_body_bytes`, `max_entries`, `max_history_per_task`, `max_source_fields`, `max_sources`). Individual users toggle the sidebar entry in **Settings → General → Task board**.

---

## Credentials

Every task-board route accepts `Authorization: Bearer <credential>`. How you obtain that credential depends on the server's authentication mode (`GET /v1/info` reports `auth_provider` and `auth_enabled`):

- **Accounts mode (`auth_provider: "accounts"`)**: use a user-owned API key declared under `api_keys` in the server's `config.yaml` with `scopes: ["tasks"]` or `scopes: ["usage", "tasks"]`, or use a normal account login/session token. A `tasks`-scoped API key reaches every task-board route except `DELETE` (`DELETE` requires a login session). A single key with `scopes: ["usage", "tasks"]` serves both usage reporting and task sources. The deprecated `OMNIGENT_USAGE_READ_TOKEN` environment variable is `usage`-only and is refused (`403`) on `/v1/me/tasks*`.
- **Other authenticated modes (`oidc`, `header`)**: `api_keys` in `config.yaml` are ignored with a startup warning. Authenticate with a login/session bearer token (or the proxy's identity header in `header` mode).
- **Authentication off (`OMNIGENT_AUTH_ENABLED=0` / single-user local server)**: no `Authorization` header is needed.

To mint a key for `accounts` mode and print its SHA-256 digest (only the digest is stored in `config.yaml`; keep the key with the feeder):

```sh
python3 -c 'import hashlib, secrets; k = secrets.token_urlsafe(32); print("key:", k); print("sha256:", hashlib.sha256(k.encode()).hexdigest())'
```

Add the digest to the server's `config.yaml`:

```yaml
api_keys:
  - name: team-board
    user: alice@example.com
    scopes: [usage, tasks]
    sha256: "<64-char hex digest>"
```

A key authorizes only the routes in its `scopes` (`DELETE /v1/me/tasks/sources/{source_id}` remains login-only); it cannot open sessions, change settings, or delete the account. Deleting or disabling the user account immediately invalidates its keys.

---

## Object and property reference

### Group envelope (`@type: "Group"`)

A feeder may send either a single `Task` object (`@type: "Task"`) or a JSCalendar `Group` containing `Task` entries. Non-`Task` entries in `entries` are ignored; duplicate task `uid` values in one `Group` are rejected (`422 duplicate_uid`).

| Property | Origin | Type & rules |
|---|---|---|
| `@type` | RFC 8984 §2.2 | Required. `"Group"`. |
| `uid` | RFC 8984 §4.1.2 | Required. Non-empty string (`1..255` chars) identifying the group. |
| `updated` | RFC 8984 §4.1.6 | Required. `UTCDateTime` (`YYYY-MM-DDTHH:MM:SS[.frac]Z`; fractional seconds must not end in `0`). |
| `title` | RFC 8984 §4.2.1 | Optional string (`<= 128` chars). |
| `source` | RFC 8984 §4.2.11 | Optional absolute URI (`http:` or `https:` becomes a link in Settings; other schemes stay plain text). |
| `entries` | RFC 8984 §5.3 | Required array of `Task` objects (`<= 10,000`). |
| `omnigent.ai:complete` | **Omnigent-defined** | Optional `"open"` or `"all"`. Declares whether omitted tasks have disappeared (see [Completeness and freshness](#completeness-and-freshness)). |

### Task properties (`@type: "Task"`)

Unknown standard JSCalendar properties (for example `recurrenceRules`, `locations`, or `Participant.sendTo`) are preserved on storage and returned on read. Sending `null` for any recognized property is rejected (`422 bad_value`); omit unset properties instead. Sending any read-only `omnigent.ai:` property (`omnigent.ai:source`, `omnigent.ai:gone`, `omnigent.ai:closedSeen`, `omnigent.ai:unresolved`) in a `PUT` body is rejected (`422 bad_value`).

| Property | Origin | Type & rules |
|---|---|---|
| `@type` | RFC 8984 §2.2 | Required. `"Task"`. |
| `uid` | RFC 8984 §4.1.2 | Required. Source-scoped stable identifier (`1..255` chars). |
| `updated` | RFC 8984 §4.1.6 | Required. `UTCDateTime` (`YYYY-MM-DDTHH:MM:SS[.frac]Z`). |
| `sequence` | RFC 8984 §4.1.7 | Optional integer (`0..9007199254740991`). Monotonic revision number used as tie-breaker when `updated` is equal. |
| `title` | RFC 8984 §4.2.1 | Optional string (`<= 500` chars). |
| `description` | RFC 8984 §4.2.2 | Optional string (`<= 20,000` chars). |
| `created` | RFC 8984 §4.1.5 | Optional `UTCDateTime`. |
| `progress` | RFC 8984 §5.2.5 | Optional `"needs-action"`, `"in-process"`, `"completed"`, `"failed"`, or `"cancelled"`. Defaults to `"needs-action"` when omitted (or derived from `participants.*.progress` per RFC 8984 §5.2.5). `"completed"`, `"failed"`, and `"cancelled"` are closed states. |
| `progressUpdated` | RFC 8984 §5.2.6 | Optional `UTCDateTime`. Used as the closed timestamp when `progress` is closed (falls back to server observation time). |
| `percentComplete` | RFC 8984 §5.2.4 | Optional integer (`0..100`). |
| `priority` | RFC 8984 §4.4.1 | Optional integer (`0..9`; `0` = unrated, `1` = highest, `9` = lowest). |
| `start` | RFC 8984 §5.2.2 | Optional `LocalDateTime` (`YYYY-MM-DDTHH:MM:SS[.frac]`, no `Z` or offset). |
| `due` | RFC 8984 §5.2.1 | Optional `LocalDateTime` (no `Z` or offset). |
| `timeZone` | RFC 8984 §4.7.1 | Optional IANA time zone name (for example `"America/Los_Angeles"` or `"Asia/Shanghai"`). |
| `participants` | RFC 8984 §4.4.6 | Optional map of participant id to `{"@type": "Participant", "name"?: string, "roles": {<role>: true}, "progress"?: Progress, "progressUpdated"?: UTCDateTime}`. Every participant must have a non-empty `roles` map. |
| `relatedTo` | RFC 8984 §4.1.8 | Optional map of target task `uid` to `{"@type": "Relation", "relation": {<type>: true}}`. Recognized relation types: `"parent"`, `"child"`, `"first"`, `"next"`, and **Omnigent-defined** `"omnigent.ai:depends-on"`. |
| `links` | RFC 8984 §4.2.7 | Optional map of link id to `{"@type": "Link", "href": URI, "rel"?: string, "title"?: string}`. A link with `rel: "alternate"` and an `http:`/`https:` `href` opens from the detail drawer. |
| `keywords` | RFC 8984 §4.2.4 | Optional map of tag string to `true`. |
| `omnigent.ai:depends-on` | **Omnigent-defined** | Relation type inside `relatedTo.<uid>.relation` (`{"omnigent.ai:depends-on": true}`) indicating this task cannot finish before the target task. |
| `omnigent.ai:key` | **Omnigent-defined** | Optional string (`<= 64` chars). Short human key such as `"ENG-123"` or `"owner/repo#12"`. |
| `omnigent.ai:sourceState` | **Omnigent-defined** | Optional string (`<= 64` chars). Raw status label in the source system (for example `"In Review"`). |
| `omnigent.ai:resolution` | **Omnigent-defined** | Optional string (`<= 64` chars). Raw close reason (for example `"done"`, `"duplicate"`, `"not_planned"`, or `"gone"`). |
| `omnigent.ai:waiting` | **Omnigent-defined** | Optional object `{"on": "user"\|"input"\|"auth"\|"dependency"\|"external", "since"?: UTCDateTime, "note"?: string (<= 500)}`. Populates the **Waiting on me** strip and filter when the task is open. |
| `omnigent.ai:group` | **Omnigent-defined** | Optional object `{"id": string (1..64), "name"?: string (<= 128)}`. Groups tasks into route/milestone cards on the **Routes** tab. |
| `omnigent.ai:next` | **Omnigent-defined** | Optional string (`<= 500` chars). Short next-step note shown on the route card and detail drawer. |
| `omnigent.ai:actions` | **Omnigent-defined** | Optional array (at most 8) of `{"kind": "new-session", "label": string (1..64), "project"?: string (<= 128), "prompt": string (<= 2000)}`. Buttons appear inside the task detail card. Selecting a task only opens its details. A button opens the new-session composer over the board, prefills the prompt and project, and never sends. Project resolves by Omnigent project ID first, then exact name; an unresolved project leaves no project preselected. Values round-trip unchanged. Unknown kinds and malformed entries are skipped when rendering; unsupported kinds and invalid values are rejected on write. With usable actions and no `alternate` link, the card omits the no-page message. |
| `omnigent.ai:workedBy` | **Omnigent-defined** | Optional array (`<= 200`) of `{"session": string (1..128), "agent"?: string (<= 64), "host"?: string (<= 128), "from": UTCDateTime, "to"?: UTCDateTime}`. Links the task to Omnigent sessions so session cards, waiting prompts, and cost rollups join across both views. |
| `omnigent.ai:sourceFields` | **Omnigent-defined** | Optional map (`<= 32` entries) of field key (`1..64` chars) to `{"label": string (1..64), "value": string (<= 2000)}` rendered in the task detail drawer. |
| `omnigent.ai:history` | **Omnigent-defined** | Optional array (`<= 5,000` per task) of append-only events `{"id": string (1..128), "time": UTCDateTime, "kind": "created"\|"state"\|"waiting"\|"assignee"\|"parent"\|"relation"\|"field"\|"comment"\|"closed"\|"reopened"\|"commit"\|"other", "actor"?: string (<= 128), "label"?: string (<= 64), "field"?: string (<= 64), "from"?: string (<= 500), "to"?: string (<= 500), "note"?: string (<= 2000)}`. Deduplicated by `(source_id, uid, id)`. |

### Read-only response properties

The server attaches these **Omnigent-defined** properties on `GET` responses (`omnigent.ai:source`, `omnigent.ai:gone`, `omnigent.ai:closedSeen`, and `omnigent.ai:unresolved` on `Task` objects are rejected with `422 bad_value` if sent in a `PUT` body; `omnigent.ai:cursor` and `omnigent.ai:changedThrough` are added on the top-level `Group` returned by `GET /v1/me/tasks`):

| Property | Origin | Meaning |
|---|---|---|
| `omnigent.ai:source` | **Omnigent-defined** | Source identifier (`{source_id}`) that owns the task. |
| `omnigent.ai:gone` | **Omnigent-defined** | Object `{"at": UTCDateTime}` attached when a complete report omitted an earlier task. |
| `omnigent.ai:closedSeen` | **Omnigent-defined** | Server `UTCDateTime` when a closed task without `progressUpdated` was first observed closed. |
| `omnigent.ai:unresolved` | **Omnigent-defined** | Array of target `uid` strings for `relatedTo` targets not present in the owner's stored tasks. |
| `omnigent.ai:cursor` | **Omnigent-defined** | Added on `GET /v1/me/tasks` when more pages remain. |
| `omnigent.ai:changedThrough` | **Omnigent-defined** | Added on `GET /v1/me/tasks?changed_after=...`: server watermark `UTCDateTime` captured when the first page was read. |

---

## Example report and commands

The following `Group` report exercises standard RFC 8984 properties and every writable **Omnigent-defined** property:

```json
{
  "@type": "Group",
  "uid": "example-eng",
  "title": "Example Engineering",
  "source": "https://tracker.example.com/projects/eng",
  "updated": "2026-11-06T07:00:00Z",
  "omnigent.ai:complete": "open",
  "entries": [
    {
      "@type": "Task",
      "uid": "eng-123",
      "sequence": 4,
      "created": "2026-11-05T09:00:00Z",
      "updated": "2026-11-06T06:58:00Z",
      "title": "Recover interrupted uploads",
      "description": "Resume chunked uploads from the last confirmed offset.",
      "progress": "in-process",
      "progressUpdated": "2026-11-06T06:30:00Z",
      "percentComplete": 60,
      "priority": 2,
      "due": "2026-11-10T18:00:00",
      "timeZone": "Asia/Shanghai",
      "keywords": {
        "storage": true,
        "reliability": true
      },
      "links": {
        "page": {
          "@type": "Link",
          "href": "https://tracker.example.com/issues/ENG-123",
          "rel": "alternate",
          "title": "ENG-123"
        }
      },
      "participants": {
        "owner": {
          "@type": "Participant",
          "name": "Alice",
          "roles": {
            "owner": true,
            "assignee": true
          },
          "progress": "in-process"
        }
      },
      "relatedTo": {
        "eng-100": {
          "@type": "Relation",
          "relation": {
            "parent": true
          }
        },
        "eng-120": {
          "@type": "Relation",
          "relation": {
            "omnigent.ai:depends-on": true
          }
        }
      },
      "omnigent.ai:key": "ENG-123",
      "omnigent.ai:sourceState": "In Review",
      "omnigent.ai:group": {
        "id": "transfer-v2",
        "name": "Transfer reliability"
      },
      "omnigent.ai:waiting": {
        "on": "dependency",
        "since": "2026-11-06T06:30:00Z",
        "note": "Waiting on ENG-120 checksum contract"
      },
      "omnigent.ai:next": "Run the interrupted-stream test suite after ENG-120 merges.",
      "omnigent.ai:actions": [
        {"kind": "new-session", "label": "Continue task", "project": "Example project", "prompt": "Review the interrupted-stream test results for ENG-123."}
      ],
      "omnigent.ai:workedBy": [
        {
          "session": "37b15a8ddf9a48e48297735f74530d25",
          "agent": "codex",
          "host": "workstation-a",
          "from": "2026-11-06T05:10:00Z"
        }
      ],
      "omnigent.ai:sourceFields": {
        "estimate": {
          "label": "Estimate",
          "value": "2d"
        }
      },
      "omnigent.ai:history": [
        {
          "id": "evt-1",
          "time": "2026-11-05T09:00:00Z",
          "kind": "created",
          "actor": "Alice"
        },
        {
          "id": "evt-2",
          "time": "2026-11-06T06:30:00Z",
          "kind": "waiting",
          "actor": "Alice",
          "to": "ENG-120",
          "note": "Blocked on checksum contract"
        }
      ]
    }
  ]
}
```

Push the report and read incremental changes with `curl`:

```sh
curl -fsS -X PUT "https://omnigent.example.com/v1/me/tasks/sources/example-eng" \
  -H "Authorization: Bearer $OMNIGENT_TASK_KEY" \
  -H "Content-Type: application/jscalendar+json" \
  --data-binary @report.json

curl -fsS "https://omnigent.example.com/v1/me/tasks?source=example-eng&changed_after=2026-11-06T00:00:00Z" \
  -H "Authorization: Bearer $OMNIGENT_TASK_KEY"
```

---

## Connecting a board feeder or script

### Board feeder (`omnigent.ai:complete: "open"`)

A file-backed or repository task board feeder (such as a collaboration kit board that keeps active items on a live board and archives older closed items) connects by sending a `Group` with `"omnigent.ai:complete": "open"` on each sync or commit hook:

1. Include every currently open task (`progress` in `"needs-action"` or `"in-process"`) plus any recently closed tasks (`"completed"`, `"failed"`, `"cancelled"`) still in the feeder's window.
2. Map each item's stable token or ticket id to `uid` and `omnigent.ai:key`, its parent or track to `omnigent.ai:group` and `relatedTo.<parent_uid>.relation.parent`, and any active blocker to `omnigent.ai:waiting` (`on: "user" | "input" | "auth" | "dependency" | "external"`).
3. Populate `omnigent.ai:workedBy` with the Omnigent session ID (`session`), `agent`, `host`, and `from`/`to` timestamps when a session claims or releases a task. The Task board automatically joins those session IDs with live Omnigent sessions, child trees, pending user question cards, and session cost readings.
4. Emit state transitions, claim changes, and close notes as `omnigent.ai:history` entries with deterministic `id` values so repeated pushes stay idempotent.

Because `omnigent.ai:complete` is `"open"`, any previously open task omitted from a later report receives `omnigent.ai:gone: {"at": "<report_time>"}` (and is excluded from default open-task reads unless `include_gone=1` or `changed_after` is passed), while previously closed tasks remain stored even after they age out of the feeder's recent-closed window.

### Script or webhook feeder (single task or partial batch)

A CI job, webhook receiver, or lightweight script that knows only about the tasks it just touched can push either:

- A single `Task` object (`"@type": "Task"`), or
- A `Group` (`"@type": "Group"`) **without** `omnigent.ai:complete`.

In both cases, tasks not mentioned in the payload are left untouched in the store.

---

## Completeness and freshness

Each `PUT /v1/me/tasks/sources/{source_id}` is atomic and returns a summary object:

```json
{
  "applied": 1,
  "unchanged": 0,
  "held": [],
  "returned": 0,
  "gone": 0,
  "complete": "applied",
  "history_added": 2
}
```

- **Revision ordering**: incoming tasks are ordered against the stored version by `(updated, sequence)` (where `sequence` defaults to `0`). When `(updated, sequence)` is greater than the stored version, the incoming task body replaces the stored body (`applied` increments).
- **Idempotent retries vs older/conflicting revisions**: when `(updated, sequence)` equals the stored version and the task body is identical, `unchanged` increments (`omnigent.ai:history` still appends any new event `id` values). When `(updated, sequence)` is older than the stored version, or equal with a different body, the stored body is kept and `{"uid": "<uid>", "reason": "older"}` is appended to `held`.
- **Completeness sweep**:
  - `"omnigent.ai:complete": "open"` marks stored **open** tasks absent from the report as gone (`omnigent.ai:gone: {"at": "<timestamp>"}`, `gone` increments, `complete: "applied"`).
  - `"omnigent.ai:complete": "all"` marks **any** stored task (open or closed) absent from the report as gone (`complete: "applied"`).
  - If a complete report carries a Group `updated` timestamp older than the source's last applied complete report, the completeness sweep is skipped (`complete: "skipped_older"`).
  - If a task previously marked `omnigent.ai:gone` reappears in a later report, `omnigent.ai:gone` is cleared and `returned` increments.

---

## Incremental reads and source removal

### Incremental change sync (`changed_after`)

External tools that mirror or index the Task board can poll `GET /v1/me/tasks?changed_after=<UTCDateTime>` instead of re-reading every task:

1. Start with `GET /v1/me/tasks?changed_after=1970-01-01T00:00:00Z`. Record `omnigent.ai:changedThrough` from the first page of the response.
2. Follow `omnigent.ai:cursor` until a page omits `omnigent.ai:cursor`. Keep the same query parameters alongside `cursor`.
3. For each returned entry, if `omnigent.ai:gone` is present, remove `(omnigent.ai:source, uid)` from your open-task cache (or mark it gone); otherwise upsert the entry. Every state change, body edit, `returned` transition, and newly appended `omnigent.ai:history` event advances the task's change timestamp so incremental readers observe it.
4. On the next poll, pass `changed_after` set to the previous run's `omnigent.ai:changedThrough` **minus 60 seconds**. Because `omnigent.ai:changedThrough` is the server timestamp when the first page's read began, re-reading the 60-second overlap window picks up writes that started before `changedThrough` and committed within that window (re-reading the overlap is harmless because merging by `(omnigent.ai:source, uid)` is idempotent; a write that takes longer than 60 seconds to commit after its timestamp is assigned would fall outside a 60-second window).
5. Also poll `GET /v1/me/tasks/sources` and compare each source's `(id, created)` pair against your cache. If a source disappears or its `created` timestamp changes, evict cached tasks for that `source_id` before applying incremental pages.

### Removing a source

Remove a source from **Settings → Task board** (**Delete source** button next to the source) or call `DELETE` with a login session token:

```sh
curl -fsS -X DELETE "https://omnigent.example.com/v1/me/tasks/sources/example-eng" \
  -H "Authorization: Bearer $OMNIGENT_LOGIN_TOKEN"
```

Deletion is scoped to the authenticated owner and removes the source row, its tasks, and its history events in one transaction. Pushing to the same `{source_id}` later creates a fresh source generation with a new `created` timestamp.
