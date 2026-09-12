# LOCAL_MODS — pandalaohe/omnigent `local/host-custom`

> Fork: pandalaohe/omnigent (running branch `local/host-custom`)
> Upstream: omnigent-ai/omnigent (`main` branch), remote literally named `upstream`
> Registry format version: 2
> Bootstrap date: 2026-09-12
> Bootstrap upstream commit hash: 40a249a27266127f1a6e125ee31894bbd5116d2e  (merge-base; canonical_base source for D2-origin mods)
> Fetched upstream/main: 6cfc09e6bf9dd418da3ac778a74b6da28e0889a2 (2026-09-11), 112 commits ahead of the merge-base
> Branch tip this registry describes: 5ab623360473fb24f476ffdef85613c3b64e6f18
> Last updated: 2026-09-12

Rebuilt from v1 (3 rows) on 2026-09-12 — board OMN04 / task T260912-016. The Windows 48-MOD ledger was never committed and could not be recovered, so every row below is **derived** from FEATURE_MAP (S01–S26, F24–F28), PR_STATUS and the branch's own history. Each row's `why` names the evidence it came from and whether the classification is an observed **fact** or a **decision** taken during the rebuild. See `## Narrative` for the method and its blind spots.

---

## Contribution Index (AUTHORITATIVE — machine-reconciled by upstream-update Phase F)

| mod_id | upstreamable | upstream_pr | upstream_issue | lifecycle | absorption | head_branch | exit-condition |
|--------|--------------|-------------|----------------|-----------|------------|-------------|----------------|
| `S01-mobile-terminal-ime` | yes | https://github.com/omnigent-ai/omnigent/pull/6910 |  | candidate | pending | codex/fix-mobile-terminal-ime | Drop when the upstream candidate #6913 (another author) or its successor lands equivalent composition and caret handling; re-verify the residual against that merged head first. Our own #6910 monolith is retired — do not re-open it as a competitor to the three-layer stack. |
| `S02-assistant-linebreaks` | yes | https://github.com/omnigent-ai/omnigent/pull/6624 |  | filed | pending | codex/pr-assistant-linebreaks-20260906 | Drop when #6624 merges. |
| `S03-windows-host-fixes` | maybe |  |  | private | pending | fix/windows-helper-root-fix | Re-verify each residual against the latest upstream head before filing: FEATURE_MAP records an upstream candidate covering two of these requirements, so wait rather than file a competing PR. Drop a residual once that candidate's merged behaviour covers it. |
| `S04-askuserquestion-wait` | maybe |  |  | private | pending |  | Remove when upstream makes the ask-user-question wait configurable, or when the hook itself checks for an attached web client before long-polling. (v1 row 1, verbatim.) |
| `S05-agent-cache` | yes | https://github.com/omnigent-ai/omnigent/pull/6170 |  | filed | pending | fix/agent-cache-concurrency | Drop when #6170 merges. The PR carries a trimmed implementation that is not identical to the custom one — reconcile behaviour before retiring the local version, do not overwrite custom with the PR's code. |
| `S06-preference-sync` | yes | https://github.com/omnigent-ai/omnigent/pull/6626 |  | filed | pending | codex/pr-user-preferences-20260906 | Drop when #6626 merges. The dependent settings groups retire only after this one, never before. |
| `S07-host-roots-picker` | yes | https://github.com/omnigent-ai/omnigent/pull/5948 |  | filed | pending | feat/host-workspace-defaults | Drop when #5948 and its follow-ups cover default workspaces and every picker entry point. Exact path semantics belong to this group alone — do not let another group re-implement them. |
| `S08-project-target-cwd` | yes | https://github.com/omnigent-ai/omnigent/pull/6627 |  | filed | pending | codex/pr-project-target-cwd-20260906 | Drop when #6627 lands the full contract — target, entry points, and success write-back. Partial coverage retires nothing. |
| `S09-dictation-punctuation` | yes | https://github.com/omnigent-ai/omnigent/pull/6625 |  | filed | pending | codex/pr-dictation-punctuation-20260906 | Drop the backend residual when #6625 merges. The web final-text queue, microphone path and the remaining consumers are still unfiled and stay in this row. |
| `S10-inline-attachments` | yes | https://github.com/omnigent-ai/omnigent/pull/6395 |  | filed | pending | feat/inline-attachment-composer | Drop when #6395 merges. The visible editor and the send closure are not yet extracted and stay in this row. |
| `S11-configurable-hotkeys` | yes | candidate |  | candidate | pending |  | File as its own PR reusing S06 for storage. Drop when upstream ships a configurable binding layer covering parsing, conflicts and the consumers. |
| `S12-tui-softkeys-touch` | yes | candidate |  | candidate | pending |  | File separately from S01 — the two must not be merged into one PR. Drop when upstream provides a direct soft-key path for terminal users. |
| `S13-mobile-assistant` | maybe |  |  | private | pending |  | Resolve `upstreamable` before filing — this presumes the fork's own mobile layout. Drop if upstream ships an equivalent mobile action surface. |
| `S14-navigation-titles` | yes | candidate |  | candidate | pending |  | File after S06 lands its storage. Drop when upstream owns the polling and visibility rules. |
| `S15-global-read-all` | yes | candidate |  | candidate | pending |  | Drop when upstream adds a bulk read entry. Never widen this row into upstream's read-state model. |
| `S16-device-layout-memory` | maybe |  |  | private | pending |  | Verify the cross-session path, then resolve `upstreamable`. Drop if upstream persists layout itself. |
| `S17-ui-font-scale` | yes | candidate |  | candidate | pending |  | Drop when upstream's own font ramp covers the mobile default. Keep only the uncovered residual — do not re-file the parts upstream already has. |
| `S18-native-plan` | yes | https://github.com/omnigent-ai/omnigent/pull/6629 |  | filed | pending | codex/native-plan-persistence-20260906 | Drop when #6629 merges with the store projection, the accordion and the refresh/reconnect chain. Persistence alone does not retire this row. |
| `S19-native-goal` | yes | https://github.com/omnigent-ai/omnigent/pull/6630 |  | candidate | pending | codex/native-goal-projection-20260906 | Locate the upstream implementation SHA that replaced #6630 and compare behaviour before retiring anything — a resolved ticket is not evidence the Goal work was absorbed. The `0098e2d46` achieved-marker hotfix has never been filed separately; confirm S19 and S21 do not both claim it. |
| `S20-codex-child-inventory` | yes | https://github.com/omnigent-ai/omnigent/pull/6393 |  | filed | pending | fix/claude-subagent-authoritative-terminal | Drop when #6393 merges. Keep separate from the Claude owner (#7106) — do not merge this row into S21. |
| `S21-claude-native-control` | yes | https://github.com/omnigent-ai/omnigent/pull/7106 | https://github.com/omnigent-ai/omnigent/issues/5687 | filed | pending | codex/claude-terminal-7-upgrade | Remove each residual only after upstream supplies equivalent correlation, ordering, replay-safe recovery and status delivery. (v1 row 2, verbatim.) #7106 carries only the sub-agent terminal-state and resume slice — Stop, reconcile, watchdog and Goal recovery are still local, so the merge of #7106 retires part of this row, never all of it. |
| `S22-deletion-claim-lock` | yes | candidate |  | candidate | pending |  | File the schema, the lease and the cross-process API as one PR before any S23 or S26 residual goes up — both depend on this contract. Drop when upstream owns the lease. |
| `S23-host-cli-retention` | yes | candidate |  | candidate | pending |  | Drop when the retention coordinator itself goes upstream (or is replaced there) with terminal outcomes and backoff; the `_stop_session_host_runner_outcome` split can be dropped the moment upstream's stop helper distinguishes an already-reaped runner from an undelivered stop. (v1 row 3, verbatim.) Depends on S22; do not fold this into the Claude-only #7106. |
| `S24-usage-context` | yes | https://github.com/omnigent-ai/omnigent/pull/6634 |  | filed | pending | codex/provider-usage-windows-20260906 | Drop the probe residual when #6634 merges. Claude-side persistence, the source fence and the settings display are still unfiled and stay in this row. Depends on S06. |
| `S25-agent-library` | yes | https://github.com/omnigent-ai/omnigent/pull/6633 |  | filed | pending | codex/pr-custom-agent-library-20260906 | Drop when #6633 and its follow-ups cover the badges, the editor and every entry point. Depends on S06. |
| `S26-archive-library` | yes | https://github.com/omnigent-ai/omnigent/pull/6628 |  | filed | pending | codex/pr-archive-library-20260906 | Split by schema / query / reader / entry point rather than re-filing the monolith; drop each part as its own PR merges. The `c732c3b25` project-filter hotfix is not in the filed head. Depends on S22. |
| `F24-custom-host-maintenance` | no |  |  | private | pending |  | None while the fork ships its own build. Revisit only if upstream adopts a custom update channel. |
| `F26-deploy-environment` | no |  |  | private | pending |  | None while the fn deployment exists. Drop a repair once no deployed database predates it. |
| `F28-alignment-residuals` | no |  |  | private | n/a |  | Drop when upstream's own release signature and retry event types make the local alignment unnecessary. |
| `local-ledger` | no |  |  | private | n/a |  | None. The ledger lives as long as the fork does, and never leaves it. |

`basis` says how the row's lifecycle fields were arrived at: **fact** = an observed lifecycle event recorded in `MAC_HANDOFF/PR_STATUS.md`; **decision** = a classification this rebuild made because no observation settled it. A decision is re-verified before it is acted on.

**Field domains** are defined by `upstream-update/templates/local-mods-init.md.tmpl`; `upstreamable=maybe` keeps `lifecycle=private` until the verdict is resolved. Point-in-time remote state — whether a pull request stood open or had landed when someone last looked, and the timestamp of that look — is deliberately absent from this whole file, Narrative included, per the template's volatile-state rule. It lives in `MAC_HANDOFF/PR_STATUS.md` and in run receipts instead.

## Committed Modifications (descriptive)

| mod_id | commits | paths | area | basis | what | why |
|--------|---------|-------|------|-------|------|-----|
| `S01-mobile-terminal-ime` | _(no single-feature commit; see Path Ownership)_ | 4 (4 primary) | web terminal (mobile) | fact (PR_STATUS) + decision (retry as three layers) | IME composition commits, caret movement and input-only symbol handling for the mobile terminal textarea. | On a phone the terminal textarea dropped input-only symbols and mis-placed the caret after an IME composition commit, so CJK input was unusable. Derived from FEATURE_MAP S01 and the fork stack #2 -> #3 -> #4. |
| `S02-assistant-linebreaks` | `a50fc9210` | 4 (4 primary) | web chat rendering | fact | Preserve single line breaks in assistant markdown output. | Markdown collapses a lone newline, so assistant text written with deliberate single-line breaks rendered as one run-on paragraph. Derived from FEATURE_MAP S02 and commit a50fc9210. |
| `S03-windows-host-fixes` | `eb6716547` `b5674a809` `0702be323` `807abcaf1` `9951d08c5` | 15 (9 primary) | host daemon, filesystem helper, Windows checkout | decision | Windows host daemon and filesystem-helper stability, drive-letter workspace paths in the folder picker, reliable service-wrapper invocation, and CRLF normalisation before the CSS parser test reads its source. | On Windows the host daemon and its filesystem helper were unreliable, drive-letter paths were rejected by the picker, and a CRLF checkout broke index.css parsing in tests. Derived from FEATURE_MAP S03 and fork PR #1, which merged into custom only. |
| `S04-askuserquestion-wait` | `ad0d92780` | 2 (0 primary) | claude-native bridge hooks | decision | PreToolUse AskUserQuestion command-hook `timeout` 10 -> 86400 (v1 row 1). The mod now lives at `omnigent/harnesses/claude_native/bridge.py:2009`; its v1 anchor `omnigent/claude_native_bridge.py` exists on neither branch. | In `bypassPermissions` mode the web-UI card had 10 s before the hook fell through to Claude's TUI picker, which a web-UI user never sees; the `PermissionRequest` fallback then denied the call. Waiting a day matches the hook's own long-poll budget (`_PERMISSION_TIMEOUT_S`) and the sibling `PermissionRequest` hook. Accepted trade-off, same as the sibling: a terminal-only bypass session, or one whose web client is unreachable, waits for the web answer instead of getting the TUI picker. (v1 row 1, verbatim.) |
| `S05-agent-cache` | _(no single-feature commit; see Path Ownership)_ | 5 (5 primary) | server runtime | fact | Keep AgentCache work off the event loop and serialize its operations per agent. | A synchronous AgentCache load ran on the server's single asyncio loop, so every concurrent request queued behind it. Derived from FEATURE_MAP S05, fork stack #6 -> #7, and the `asyncio.to_thread` residual in `routes_hooks.py`. |
| `S06-preference-sync` | _(no single-feature commit; see Path Ownership)_ | 8 (8 primary) | server + web preferences | fact | User-preference storage, its API, and per-client identity isolation. | Every other settings group needs one owner for preference storage and client identity; without it each feature invented its own. Derived from FEATURE_MAP S06, which names this group as the owner S11 / S14 / S24 / S25 depend on. |
| `S07-host-roots-picker` | `256dc42a5` `f97b27f83` `f31c7677b` `11b3a947e` `9a0eb7902` `4e59b1a37` | 40 (30 primary) | hosts API + web workspace picker | fact | Browsable filesystem roots behind a capability, default workspaces, native path retention across browse routes, unified picker entry points, and per-host workspace pins. | The host exposed no way to browse its filesystem, so a workspace had to be typed exactly, and native path semantics were lost across browse routes. Derived from FEATURE_MAP S07. |
| `S08-project-target-cwd` | _(no single-feature commit; see Path Ownership)_ | 12 (12 primary) | web new-session flow | fact | Explicit project and workspace context when creating a session, with prefill and success write-back. | A new session's project and cwd were implicit, so a session could silently land in the wrong workspace. Derived from FEATURE_MAP S08; depends on S07. |
| `S09-dictation-punctuation` | `3e1230959` | 19 (17 primary) | dictation worker + web final-text queue | fact | Restore punctuation on final browser transcripts, plus a `.gitattributes` `eol=lf` pin for the model-fetch script. | Browser dictation emitted unpunctuated final text. Derived from FEATURE_MAP S09. The eol pin belongs here because the dictation model-fetch script is executed directly by Bash from Windows checkouts — a residual shared with S03. |
| `S10-inline-attachments` | `7d3fc32a5` `94f2fa280` | 32 (21 primary) | web composer + runtime | fact | Preserve inline attachment positions and the composer placeholder selector. | Attachments were appended at the end, losing the caret position they were dropped at, so the model received a different content order than the user wrote. Derived from FEATURE_MAP S10. |
| `S11-configurable-hotkeys` | `3f319cfd2` | 22 (19 primary) | web shortcuts | decision | User-editable keyboard shortcuts — parsing, conflict handling, the editor, the command palette, and every consumer hook. | Shortcuts were hard-coded, so a binding that collided with a browser or OS shortcut could not be moved. Derived from FEATURE_MAP S11, which found no owner PR upstream. |
| `S12-tui-softkeys-touch` | _(no single-feature commit; see Path Ownership)_ | 2 (2 primary) | web terminal | decision | Soft-key and touch dispatch into the terminal view. | A phone has no Ctrl or Esc, so a TUI running in the terminal was unusable without on-screen keys. Derived from FEATURE_MAP S12, which found no owner PR and requires this stay separate from the already-filed IME work. |
| `S13-mobile-assistant` | _(no single-feature commit; see Path Ownership)_ | 7 (7 primary) | web mobile | decision | Floating two-dimension mobile assistant — layout, hit testing, dispatch and dock, settings ordering, and keeping the keyboard-shortcuts settings page reachable on mobile so the assistant can be configured there. | Core actions were unreachable on a phone. Derived from FEATURE_MAP S13, which found no owner PR and records dependencies on S11 and the terminal owner. |
| `S14-navigation-titles` | _(no single-feature commit; see Path Ownership)_ | 11 (11 primary) | web navigation | decision | Navigation polling and window/background rules, mobile titles, open-latest on session open, unseen-conversation state, the breadcrumb and the native server switcher. | Background polling and titles behaved wrongly while the window was hidden, and opening a session did not land at the latest turn. Derived from FEATURE_MAP S14, which found no owner PR and records reuse of S06. |
| `S15-global-read-all` | _(no single-feature commit; see Path Ownership)_ | 1 (1 primary) | web sidebar row actions | decision | The bulk mark-all-read entry. | Read state could only be cleared one conversation at a time. Derived from FEATURE_MAP S15, which found no owner PR and is explicit that only the bulk entry and the residual behaviour are submitted — never a rewrite of upstream's read-state. |
| `S16-device-layout-memory` | `7a5a9f16b` | 4 (2 primary) | web layout | decision | Per-device layout memory for the resizable inline panel and rail width. | Panel widths reset on every load and did not travel per device. Derived from FEATURE_MAP S16, which found no owner PR and asks for the cross-session path to be verified first. |
| `S17-ui-font-scale` | _(no single-feature commit; see Path Ownership)_ | 3 (3 primary) | web theming | decision | UI font size applied beyond the desktop path (`applyDesktopUiFontSize` -> `applyUiFontSize`) and the font preference module. | The font-size preference applied only on desktop, so the mobile and embedded surfaces ignored it. Derived from FEATURE_MAP S17, which limits this row to the residual upstream has not covered. |
| `S18-native-plan` | _(no single-feature commit; see Path Ownership)_ | 6 (6 primary) | server + web plan | fact | Persist native Plan state across server restarts, validate forwarded session todos, and the chat plan accordion. | Plan state lived in memory, so a server restart erased a running plan. Derived from FEATURE_MAP S18; `omnigent/session_todos.py` states its own purpose as validation for native-harness session plans forwarded to the Web UI. |
| `S19-native-goal` | `9d3a3c742` `05a08c18a` `723825b36` `0098e2d46` | 35 (6 primary) | server + web goal | fact (PR filed) + decision (explicit retry) | Surface and persist native Goal sessions, frame an active goal in the chat column, report goals, and clear goal markers on achieved events. | Goal state was neither persisted nor fenced, so a goal survived as a stale marker after it was achieved. Derived from FEATURE_MAP S19. |
| `S20-codex-child-inventory` | _(no single-feature commit; see Path Ownership)_ | 8 (8 primary) | codex-native harness | fact | Bounded Codex child inventories — app server, bridge, forwarder, executor and model catalog. | Codex child sessions had no bounded inventory, so a parent could not enumerate them reliably. Derived from FEATURE_MAP S20, which scopes this row to the narrow Codex contract only. |
| `S21-claude-native-control` | `1eb1e5fa3` `de642a25e` `fcb53cbe4` `e55764083` `f18205cdd` `ab9dbc3a7` `d5dcfacf3` `32b34df34` `aebcd9f49` `de33b67d4` `c19e4cc45` `fc67e6bc5` `8e83e0e50` `b44e5aabd` `421ce928f` `351302ea4` `cf0a60a07` `c87a972a5` `85fb2abfd` | 100 (92 primary) | claude-native harness, runner, server sessions, web status | fact | Correlate terminal evidence by task id, then spawn id; retain parked aliases, timestamps, replay flags and parent read boundaries. Parse foreground Agent results, preserve distinct native completions, and merge duplicate reconstructed snapshots. Persist accepted resume ordering and retry status corrections after restart. Repair old running snapshots per child, independently retry missing transcripts/metadata, and preserve acknowledged historical prompts without creating live activity. Keep custom v2 recovery, registration watermarks and richer terminal statuses. Also carries the explicit reconcile control, background-activity distinction and stop action for unverified busy subagents. (v1 row 2, extended.) | Completion could remain running after a Host update when a result was deduplicated away, parked before metadata, or carried a resumed Agent call id. Upgrade recovery must also avoid re-enqueuing old completions or treating a consumed replay as a fresh resume. The contribution is Part of upstream issue #5687; installed Host acceptance remains separate from source proof. (v1 row 2, verbatim.) |
| `S22-deletion-claim-lock` | _(no single-feature commit; see Path Ownership)_ | 1 (1 primary) | db schema | decision | The deletion-claim / lease column that S23 and S26 both consume. | Concurrent delete and archive-close paths had no cross-process lease, so two processes could act on the same conversation. Derived from FEATURE_MAP S22, which records S23 and S26 as sharing this contract. |
| `S23-host-cli-retention` | `0e9068177` `726a868d0` `166472540` `b96ec1667` `8468d526e` `19a4aa67b` `f72a14fd7` `5ab623360` | 63 (38 primary) | server archive-close coordinator, CLI release store, session routes, conversation store, CLI | decision | Durable host retention policy and the archive-close coordinator: terminal outcomes, backoff and de-amplification (`_stop_session_host_runner_outcome` reporting `acked` / `unknown_runner` / `unavailable`; `_archive_stop_one` completing a binding with no host or an unknown one; `_execute_intent` completing an intent whose host row is gone; retry delay `clamp(age/4, 15 s, 1 h)`; one due sibling scheduled instead of a global sweep; `_expand_archive_root` honouring `next_attempt_at`; `list_pending_archive_closes` fetching roots with `get_conversations`). Also the CLI's `runner_binding_token`, native terminal recovery without re-entering the lifecycle lock, codex runner startup cancellation, and stopped-session recovery on mobile. (v1 row 3, extended.) | 89 archive intents whose captured runner was already gone retried forever every 15 s, and each processed intent re-swept every pending root with N+1 conversation queries — the fn server's single asyncio loop sat at 99.9 % CPU and every HTTP request and terminal keystroke queued behind it (TTFB 1.3-3.1 s). (v1 row 3, verbatim.) FEATURE_MAP S23 additionally names `166472540` / `b96ec1667` / `8468d526e` as this group's shipped source, not yet extracted as a PR. |
| `S24-usage-context` | _(no single-feature commit; see Path Ownership)_ | 20 (20 primary) | server usage limits + web context indicator | fact | Provider usage limits and the automatic-compact point projected onto the `session.usage` event, the context-usage settings page, cost formatting, and the stable-limits hook. | Context and provider usage were never projected to the client, so a user could not see how close a session was to compaction or to a provider cap. Derived from FEATURE_MAP S24; the residual in `web/src/lib/sse.ts` and `events.ts` is the wire contract for both fields. |
| `S25-agent-library` | `c1e143896` `08276393f` | 66 (48 primary) | server builtin agents + web agent library | fact | A reusable custom Agent library — storage, routes, the library UI, badges and editor entry points, hardened alongside usage and Host self-update. | Custom agents had no shared library, so every session re-declared them. Derived from FEATURE_MAP S25, which records #6633 as filing the library reordering only. |
| `S26-archive-library` | `7e5f92fe0` `c0f827088` `2873e0c4a` `1b1e27a24` `2e3f454c6` `c732c3b25` | 63 (50 primary) | server archive + web library | fact | A searchable, responsive Archive Library — bounded pagination and server facets, date dimensions and rolling presets, transcript search, the archive-from-menu flow with same-tick duplicate suppression, and per-viewer project filters. | Archived sessions were unreachable except by a direct link. Derived from FEATURE_MAP S26, which records that the old 65-file monolith is not the final stack. |
| `F24-custom-host-maintenance` | `1d6736bc4` `7c7ce9b03` `9ccc2c4d9` `18b426fa3` `b87cb8af3` | 13 (11 primary) | host update channel, service lifecycle, db lineage migrations | decision | The private custom update channel and its slash-bearing refs, mixed-generation Mac self-update, the launchd shutdown wait, and the migrations that join the custom and upstream schema lineages. | This fork ships its own build to every host and to the fn server, so it needs an update channel and lineage migrations upstream has no reason to carry. Derived from FEATURE_MAP F24, which records custom update and compatibility maintenance as deliberately unfiled. |
| `F26-deploy-environment` | `eb654e7ec` | 6 (6 primary) | docker entrypoint, databricks deploy | decision | Legacy schema repair on container startup and deployment-local adjustments. | The fn server's container starts from a database predating the current schema and must repair it before serving. Derived from FEATURE_MAP F26, which records environment configuration and history migration as not published. |
| `F28-alignment-residuals` | `28a555ae7` `afb2fd534` | 3 (1 primary) | runner background titles, session event routes | decision | The `release(...) -> None` to `-> bool \| None` return-type change and the matching retry event types. | After upstream absorbed the background-title feature (#6171, merged), the local retry and event types no longer lined up with the release signature. Derived from commit `afb2fd534 align title release and retry event types`; FEATURE_MAP F28 assigns alignment fixes to the original feature's owner. This row explicitly does NOT claim #6171's own feature — that is upstream's. |
| `local-ledger` | `0a4ed9de7` | 1 (1 primary) | fork ledger | fact | This file. | The fork's own contribution ledger. It is a residual path of the branch, so the READY gate requires it to be owned by a row, but it is never itself a contribution. Derived from `share/rules/upstream-fork.md`, which forbids the ledger from ever being included in an outbound PR branch. |

## Previously Applied (Now in Upstream or Superseded)

| mod_id | What | Merged in | Notes |
|--------|------|-----------|-------|

_Empty: no row has yet been retired on verified blob parity or an explicit human confirm. `F28-alignment-residuals` is adjacent to a merged upstream feature but is a local alignment residual, not that feature._

## Path Ownership

Every path of `git diff --name-only upstream/main...local/host-custom` at `5ab623360473fb24f476ffdef85613c3b64e6f18` — **443 paths, none unowned**. `primary` is the row that would carry the path in a PR cut; `also` lists the other rows whose residual lives in the same file. A row's full path list is every line where it appears in either column, so the rows' lists overlap and their sizes sum to more than 443.

`evidence` records how `primary` was decided: `diff-read` the hunk was read by hand; `commit` the path's single-feature commits agree; `path` the filename names the feature; `last-commit` several features touch an aggregate file and the most recent intent won — that tier is a PR-cut hint, not an ownership claim.

| path | primary | also | evidence |
|------|---------|------|----------|
| `.gitattributes` | `S09-dictation-punctuation` |  | commit |
| `LOCAL_MODS.md` | `local-ledger` | `S04-askuserquestion-wait`, `S21-claude-native-control`, `S23-host-cli-retention` | path |
| `NOTICE` | `F26-deploy-environment` |  | path |
| `deploy/databricks/src/app.py` | `F26-deploy-environment` |  | path |
| `deploy/docker/entrypoint.py` | `F26-deploy-environment` |  | commit |
| `designs/main-session-cli-retention.md` | `S23-host-cli-retention` |  | commit |
| `designs/server-dictation.md` | `S09-dictation-punctuation` |  | commit |
| `omnigent/chat.py` | `S23-host-cli-retention` |  | diff-read |
| `omnigent/claude_native_status_probe.py` | `S21-claude-native-control` |  | commit |
| `omnigent/cli.py` | `S03-windows-host-fixes` | `F24-custom-host-maintenance`, `S25-agent-library` | last-commit |
| `omnigent/cli_retention.py` | `S23-host-cli-retention` |  | commit |
| `omnigent/codex_rate_limits.py` | `S25-agent-library` |  | commit |
| `omnigent/db/db_models.py` | `S25-agent-library` | `S07-host-roots-picker`, `S23-host-cli-retention` | last-commit |
| `omnigent/db/migrations/versions/a09c20260909_merge_custom_and_current_upstream.py` | `F24-custom-host-maintenance` |  | path |
| `omnigent/db/migrations/versions/a10c20260910_merge_custom_retention_and_upstream.py` | `S23-host-cli-retention` |  | path |
| `omnigent/db/migrations/versions/f6a1b2c3d4e5_add_host_default_workspace.py` | `S07-host-roots-picker` |  | commit |
| `omnigent/db/migrations/versions/f7a1b2c3d4e5_add_user_preferences.py` | `S06-preference-sync` |  | path |
| `omnigent/db/migrations/versions/f8a1b2c3d4e5_add_archived_at.py` | `S26-archive-library` |  | path |
| `omnigent/db/migrations/versions/f9a1b2c3d4e5_add_deletion_claim.py` | `S22-deletion-claim-lock` |  | path |
| `omnigent/db/migrations/versions/fa1b2c3d4e5_add_provider_usage_limits_metadata.py` | `S24-usage-context` |  | path |
| `omnigent/db/migrations/versions/fb1b2c3d4e5_add_session_todos_metadata.py` | `S18-native-plan` |  | diff-read |
| `omnigent/db/migrations/versions/fc1b2c3d4e5_reconcile_connections_for_legacy_custom_lineage.py` | `F24-custom-host-maintenance` |  | commit |
| `omnigent/db/migrations/versions/fd1b2c3d4e5_add_custom_agent_library.py` | `S25-agent-library` |  | commit |
| `omnigent/db/migrations/versions/fe1b2c3d4e5_add_host_cli_retention_policy.py` | `S23-host-cli-retention` |  | commit |
| `omnigent/db/migrations/versions/ff1b2c3d4e5_add_archive_close_intent.py` | `S23-host-cli-retention` |  | commit |
| `omnigent/db/utils.py` | `S26-archive-library` |  | commit |
| `omnigent/entities/conversation.py` | `S21-claude-native-control` | `S23-host-cli-retention`, `S26-archive-library` | last-commit |
| `omnigent/harnesses/claude_native/bridge.py` | `S21-claude-native-control` | `S19-native-goal`, `S04-askuserquestion-wait` | path |
| `omnigent/harnesses/claude_native/forwarder.py` | `S21-claude-native-control` |  | commit |
| `omnigent/harnesses/claude_native/main.py` | `S21-claude-native-control` |  | path |
| `omnigent/harnesses/claude_native/status.py` | `S21-claude-native-control` |  | path |
| `omnigent/harnesses/claude_native/status_file.py` | `S21-claude-native-control` |  | path |
| `omnigent/harnesses/codex_native/app_server.py` | `S20-codex-child-inventory` |  | path |
| `omnigent/harnesses/codex_native/bridge.py` | `S20-codex-child-inventory` |  | path |
| `omnigent/harnesses/codex_native/forwarder.py` | `S20-codex-child-inventory` |  | path |
| `omnigent/harnesses/codex_native/main.py` | `S20-codex-child-inventory` |  | path |
| `omnigent/host/connect.py` | `S07-host-roots-picker` | `S25-agent-library` | last-commit |
| `omnigent/host/frames.py` | `S25-agent-library` |  | commit |
| `omnigent/host/service.py` | `F24-custom-host-maintenance` |  | commit |
| `omnigent/host/windows_custom_update.ps1` | `F24-custom-host-maintenance` |  | commit |
| `omnigent/inner/acp_executor.py` | `S10-inline-attachments` |  | commit |
| `omnigent/inner/claude_native_executor.py` | `S10-inline-attachments` |  | commit |
| `omnigent/inner/codex_executor.py` | `S20-codex-child-inventory` |  | path |
| `omnigent/inner/goose_executor.py` | `S10-inline-attachments` |  | commit |
| `omnigent/inner/os_env.py` | `S03-windows-host-fixes` |  | commit |
| `omnigent/inner/qwen_executor.py` | `S10-inline-attachments` |  | commit |
| `omnigent/native/_native_post_delivery.py` | `S21-claude-native-control` |  | path |
| `omnigent/native_subagent_snapshot.py` | `S21-claude-native-control` |  | path |
| `omnigent/provider_usage_limits.py` | `S24-usage-context` |  | path |
| `omnigent/resources/examples/codex-sdk.yaml` | `S25-agent-library` |  | commit |
| `omnigent/runner/app.py` | `S21-claude-native-control` | `S19-native-goal`, `S23-host-cli-retention` | last-commit |
| `omnigent/runner/background_titles/service.py` | `F28-alignment-residuals` |  | commit |
| `omnigent/runner/identity.py` | `S06-preference-sync` |  | path |
| `omnigent/runner/native/interrupt.py` | `S23-host-cli-retention` | `S19-native-goal` | last-commit |
| `omnigent/runner/native/orchestration.py` | `S21-claude-native-control` | `S23-host-cli-retention` | last-commit |
| `omnigent/runner/resource_registry.py` | `S21-claude-native-control` | `S23-host-cli-retention` | last-commit |
| `omnigent/runner/session_init_protocol.py` | `S23-host-cli-retention` |  | commit |
| `omnigent/runner/session_runtime_lifecycle.py` | `S23-host-cli-retention` |  | commit |
| `omnigent/runner/tool_dispatch.py` | `S21-claude-native-control` |  | commit |
| `omnigent/runtime/agent_cache.py` | `S05-agent-cache` |  | path |
| `omnigent/runtime/harnesses/process_manager.py` | `S23-host-cli-retention` |  | commit |
| `omnigent/runtime/pending_elicitations.py` | `S21-claude-native-control` |  | commit |
| `omnigent/runtime/pending_inputs.py` | `S10-inline-attachments` |  | commit |
| `omnigent/runtime/session_stream.py` | `S21-claude-native-control` |  | commit |
| `omnigent/runtime/subagent_block_notifier.py` | `S21-claude-native-control` |  | commit |
| `omnigent/server/app.py` | `S25-agent-library` | `S09-dictation-punctuation`, `S23-host-cli-retention` | last-commit |
| `omnigent/server/archive_close.py` | `S23-host-cli-retention` |  | commit |
| `omnigent/server/cli_release_store.py` | `S23-host-cli-retention` |  | commit |
| `omnigent/server/cli_retention.py` | `S23-host-cli-retention` |  | commit |
| `omnigent/server/custom_agent_bundles.py` | `S25-agent-library` |  | commit |
| `omnigent/server/custom_agents_store.py` | `S25-agent-library` |  | commit |
| `omnigent/server/dictation.py` | `S09-dictation-punctuation` |  | commit |
| `omnigent/server/dictation_worker.py` | `S09-dictation-punctuation` |  | path |
| `omnigent/server/native_subagent_watchdog.py` | `S21-claude-native-control` |  | path |
| `omnigent/server/routes/_sessions/common.py` | `S21-claude-native-control` | `S19-native-goal` | last-commit |
| `omnigent/server/routes/_sessions/helpers.py` | `S21-claude-native-control` | `S10-inline-attachments`, `S19-native-goal`, `S23-host-cli-retention` | last-commit |
| `omnigent/server/routes/_sessions/orchestration.py` | `S21-claude-native-control` | `S10-inline-attachments`, `S19-native-goal`, `S23-host-cli-retention`, `S25-agent-library`, `S26-archive-library` | last-commit |
| `omnigent/server/routes/_sessions/subagent_reconciliation.py` | `S21-claude-native-control` |  | commit |
| `omnigent/server/routes/_workspace_validation.py` | `S07-host-roots-picker` | `S03-windows-host-fixes` | last-commit |
| `omnigent/server/routes/builtin_agents.py` | `S25-agent-library` |  | path |
| `omnigent/server/routes/codex/sessions.py` | `S19-native-goal` |  | commit |
| `omnigent/server/routes/custom_agents.py` | `S25-agent-library` |  | commit |
| `omnigent/server/routes/dictation.py` | `S09-dictation-punctuation` |  | commit |
| `omnigent/server/routes/host_tunnel.py` | `S06-preference-sync` |  | path |
| `omnigent/server/routes/hosts.py` | `S03-windows-host-fixes` | `S07-host-roots-picker`, `S23-host-cli-retention` | last-commit |
| `omnigent/server/routes/sessions/__init__.py` | `S23-host-cli-retention` |  | commit |
| `omnigent/server/routes/sessions/routes_agent.py` | `S25-agent-library` |  | path |
| `omnigent/server/routes/sessions/routes_core.py` | `S21-claude-native-control` | `S23-host-cli-retention`, `S25-agent-library`, `S26-archive-library` | last-commit |
| `omnigent/server/routes/sessions/routes_events.py` | `S21-claude-native-control` | `F28-alignment-residuals`, `S19-native-goal`, `S23-host-cli-retention` | last-commit |
| `omnigent/server/routes/sessions/routes_hooks.py` | `S05-agent-cache` |  | diff-read |
| `omnigent/server/routes/sessions/routes_items.py` | `S26-archive-library` | `S21-claude-native-control` | last-commit |
| `omnigent/server/routes/sessions/routes_resources.py` | `S21-claude-native-control` |  | commit |
| `omnigent/server/runner_session_init.py` | `S23-host-cli-retention` |  | commit |
| `omnigent/server/schemas.py` | `S21-claude-native-control` | `S19-native-goal`, `S25-agent-library`, `S26-archive-library` | last-commit |
| `omnigent/server/user_preferences_store.py` | `S25-agent-library` |  | commit |
| `omnigent/session_todos.py` | `S18-native-plan` |  | diff-read |
| `omnigent/stores/agent_store/__init__.py` | `S25-agent-library` |  | commit |
| `omnigent/stores/agent_store/sqlalchemy_store.py` | `S25-agent-library` |  | commit |
| `omnigent/stores/conversation_store/__init__.py` | `S21-claude-native-control` | `S23-host-cli-retention`, `S26-archive-library` | last-commit |
| `omnigent/stores/conversation_store/sqlalchemy_store.py` | `S26-archive-library` | `S21-claude-native-control`, `S23-host-cli-retention`, `S25-agent-library` | last-commit |
| `omnigent/stores/host_store.py` | `S07-host-roots-picker` | `S23-host-cli-retention` | last-commit |
| `omnigent/terminals/pane_reaper.py` | `S23-host-cli-retention` |  | commit |
| `omnigent/terminals/registry.py` | `S23-host-cli-retention` |  | commit |
| `omnigent/update_check.py` | `F24-custom-host-maintenance` |  | commit |
| `openapi.json` | `S21-claude-native-control` | `S07-host-roots-picker`, `S09-dictation-punctuation`, `S19-native-goal`, `S23-host-cli-retention`, `S25-agent-library`, `S26-archive-library` | last-commit |
| `pyproject.toml` | `F24-custom-host-maintenance` |  | commit |
| `scripts/fetch-dictation-models.sh` | `S09-dictation-punctuation` |  | commit |
| `tests/cli/test_backend.py` | `F24-custom-host-maintenance` | `S25-agent-library` | last-commit |
| `tests/cli/test_chat.py` | `S23-host-cli-retention` |  | diff-read |
| `tests/cli/test_host_daemon_env.py` | `S03-windows-host-fixes` |  | commit |
| `tests/cli/test_update_check.py` | `F24-custom-host-maintenance` |  | commit |
| `tests/codex_parity/test_codex_goal.py` | `S19-native-goal` |  | commit |
| `tests/db/test_migration_archive_close_intent.py` | `S23-host-cli-retention` |  | commit |
| `tests/db/test_migration_archived_at_backfill.py` | `S26-archive-library` |  | path |
| `tests/db/test_migration_connections.py` | `S23-host-cli-retention` | `F24-custom-host-maintenance` | last-commit |
| `tests/db/test_migration_custom_upstream_join.py` | `F24-custom-host-maintenance` |  | diff-read |
| `tests/db/test_migration_host_cli_retention.py` | `S23-host-cli-retention` |  | commit |
| `tests/db/test_migration_provider_usage_limits.py` | `S24-usage-context` |  | path |
| `tests/db/test_migration_session_todos.py` | `S18-native-plan` |  | diff-read |
| `tests/db/test_migration_user_preferences.py` | `S06-preference-sync` |  | path |
| `tests/db/test_utils.py` | `S26-archive-library` |  | commit |
| `tests/deploy/test_databricks_app_lakebase_cold_start.py` | `F26-deploy-environment` |  | path |
| `tests/deploy/test_databricks_web_ui.py` | `F26-deploy-environment` |  | path |
| `tests/deploy/test_docker_entrypoint_import.py` | `F26-deploy-environment` |  | commit |
| `tests/e2e/test_archive_library_filter_e2e.py` | `S26-archive-library` |  | commit |
| `tests/e2e/test_user_preferences_api_e2e.py` | `S06-preference-sync` |  | path |
| `tests/e2e_ui/chat/test_native_goal_projection.py` | `S19-native-goal` |  | commit |
| `tests/e2e_ui/chat/test_plan_tracker.py` | `S18-native-plan` |  | path |
| `tests/e2e_ui/composer/test_inline_attachment_order.py` | `S10-inline-attachments` |  | path |
| `tests/e2e_ui/messages/test_assistant_line_breaks.py` | `S02-assistant-linebreaks` |  | path |
| `tests/e2e_ui/mobile/test_ios_switcher_in_header.py` | `S14-navigation-titles` |  | path |
| `tests/e2e_ui/sessions/test_archived_date_filter.py` | `S26-archive-library` |  | commit |
| `tests/e2e_ui/sessions/test_archived_project_filter.py` | `S26-archive-library` |  | path |
| `tests/e2e_ui/shells/test_terminal_ime_composition.py` | `S01-mobile-terminal-ime` |  | path |
| `tests/e2e_ui/start_session/test_project_config_prefill.py` | `S08-project-target-cwd` |  | path |
| `tests/e2e_ui/start_session/test_windows_workspace_picker.py` | `S07-host-roots-picker` | `S03-windows-host-fixes` | path |
| `tests/e2e_ui/visual/snapshots/test_storybook_snapshot/test_story_matches_baseline/test_story_matches_baseline[chromium-components-workspace-workspacepicker--populated-with-conflict][linux].png` | `S07-host-roots-picker` |  | commit |
| `tests/e2e_ui/visual/snapshots/test_storybook_snapshot/test_story_matches_baseline/test_story_matches_baseline[chromium-components-workspace-workspacepicker--typed-filter][linux].png` | `S07-host-roots-picker` |  | commit |
| `tests/entities/test_conversation_extended.py` | `S21-claude-native-control` |  | commit |
| `tests/host/test_connect.py` | `S07-host-roots-picker` | `S25-agent-library` | last-commit |
| `tests/host/test_custom_update.py` | `F24-custom-host-maintenance` | `S03-windows-host-fixes` | path |
| `tests/host/test_frames.py` | `S25-agent-library` |  | commit |
| `tests/host/test_service.py` | `F24-custom-host-maintenance` |  | commit |
| `tests/inner/test_acp_executor.py` | `S10-inline-attachments` |  | commit |
| `tests/inner/test_claude_native_executor.py` | `S10-inline-attachments` |  | commit |
| `tests/inner/test_codex_model_catalog.py` | `S20-codex-child-inventory` |  | path |
| `tests/inner/test_goose_executor.py` | `S10-inline-attachments` |  | commit |
| `tests/inner/test_os_env.py` | `S03-windows-host-fixes` |  | commit |
| `tests/inner/test_qwen_executor.py` | `S10-inline-attachments` |  | commit |
| `tests/runner/conftest.py` | `S23-host-cli-retention` |  | commit |
| `tests/runner/test_app_native_subagent_status_probe.py` | `S21-claude-native-control` |  | commit |
| `tests/runner/test_app_sessions_native_events_lifecycle.py` | `S21-claude-native-control` |  | path |
| `tests/runner/test_app_sessions_native_supervision.py` | `S21-claude-native-control` | `S19-native-goal` | last-commit |
| `tests/runner/test_app_sessions_native_terminals_autocreate.py` | `S21-claude-native-control` |  | commit |
| `tests/runner/test_app_sessions_native_terminals_runtime.py` | `S21-claude-native-control` |  | path |
| `tests/runner/test_app_sessions_native_workflow_init.py` | `S21-claude-native-control` |  | commit |
| `tests/runner/test_cli_retention.py` | `S23-host-cli-retention` |  | commit |
| `tests/runner/test_comment_relay.py` | `S21-claude-native-control` |  | commit |
| `tests/runner/test_native_interrupt_runner.py` | `S23-host-cli-retention` | `S19-native-goal` | last-commit |
| `tests/runner/test_native_subagent_inbox_delivery.py` | `S21-claude-native-control` |  | path |
| `tests/runner/test_native_turn_recovery.py` | `S23-host-cli-retention` |  | commit |
| `tests/runner/test_resource_registry.py` | `S21-claude-native-control` |  | commit |
| `tests/runner/test_runner_dispatch.py` | `S21-claude-native-control` | `S23-host-cli-retention` | last-commit |
| `tests/runner/test_session_resources.py` | `S21-claude-native-control` |  | commit |
| `tests/runner/test_session_runtime_lifecycle.py` | `S23-host-cli-retention` |  | commit |
| `tests/runtime/harnesses/test_process_manager.py` | `S23-host-cli-retention` |  | commit |
| `tests/runtime/test_agent_cache.py` | `S05-agent-cache` |  | path |
| `tests/runtime/test_agent_cache_concurrency.py` | `S05-agent-cache` |  | path |
| `tests/runtime/test_pending_elicitations.py` | `S21-claude-native-control` |  | commit |
| `tests/runtime/test_pending_inputs.py` | `S10-inline-attachments` |  | commit |
| `tests/runtime/test_subagent_block_notifier.py` | `S21-claude-native-control` |  | commit |
| `tests/server/integration/test_agent_cache_responsiveness.py` | `S05-agent-cache` |  | path |
| `tests/server/integration/test_hosts_api.py` | `S23-host-cli-retention` | `S07-host-roots-picker` | last-commit |
| `tests/server/integration/test_hosts_filesystem.py` | `S03-windows-host-fixes` | `S07-host-roots-picker` | last-commit |
| `tests/server/integration/test_sessions_archive.py` | `S23-host-cli-retention` |  | commit |
| `tests/server/integration/test_sessions_child_sessions.py` | `S21-claude-native-control` |  | commit |
| `tests/server/integration/test_sessions_endpoints.py` | `S21-claude-native-control` | `S19-native-goal`, `S26-archive-library` | last-commit |
| `tests/server/routes/test_child_session_summary_labels.py` | `S21-claude-native-control` |  | commit |
| `tests/server/routes/test_dictation.py` | `S09-dictation-punctuation` |  | commit |
| `tests/server/routes/test_inline_attachment_order.py` | `S10-inline-attachments` |  | commit |
| `tests/server/routes/test_provider_usage_snapshot_fallback.py` | `S24-usage-context` |  | path |
| `tests/server/routes/test_session_resources.py` | `S21-claude-native-control` |  | commit |
| `tests/server/routes/test_session_updates_ws.py` | `S21-claude-native-control` |  | commit |
| `tests/server/routes/test_sessions_background_task_status.py` | `S21-claude-native-control` |  | commit |
| `tests/server/routes/test_sessions_fork.py` | `S25-agent-library` |  | commit |
| `tests/server/routes/test_sessions_project_membership.py` | `S26-archive-library` |  | commit |
| `tests/server/routes/test_sessions_runner_relay.py` | `S21-claude-native-control` |  | diff-read |
| `tests/server/routes/test_sessions_snapshot.py` | `S21-claude-native-control` |  | commit |
| `tests/server/routes/test_subagent_reconciliation.py` | `S21-claude-native-control` |  | commit |
| `tests/server/routes/test_workspace_validation_helpers.py` | `S03-windows-host-fixes` | `S07-host-roots-picker` | last-commit |
| `tests/server/test_app.py` | `S25-agent-library` |  | commit |
| `tests/server/test_cli_release_intents.py` | `S23-host-cli-retention` |  | commit |
| `tests/server/test_cli_retention.py` | `S23-host-cli-retention` |  | commit |
| `tests/server/test_custom_agents.py` | `S25-agent-library` |  | commit |
| `tests/server/test_dictation_engine.py` | `S09-dictation-punctuation` |  | commit |
| `tests/server/test_dictation_remote.py` | `S09-dictation-punctuation` |  | path |
| `tests/server/test_native_subagent_watchdog.py` | `S21-claude-native-control` |  | commit |
| `tests/server/test_runner_session_init.py` | `S23-host-cli-retention` |  | commit |
| `tests/server/test_schemas.py` | `S21-claude-native-control` |  | commit |
| `tests/server/test_session_live_state.py` | `S21-claude-native-control` |  | commit |
| `tests/server/test_user_preferences.py` | `S25-agent-library` |  | commit |
| `tests/stores/test_conversation_store.py` | `S25-agent-library` | `S21-claude-native-control`, `S26-archive-library` | last-commit |
| `tests/stores/test_host_store.py` | `S07-host-roots-picker` | `S23-host-cli-retention` | last-commit |
| `tests/stores/test_native_reasoning_recovery.py` | `S21-claude-native-control` |  | path |
| `tests/terminals/test_pane_reaper.py` | `S23-host-cli-retention` |  | commit |
| `tests/terminals/test_registry.py` | `S23-host-cli-retention` |  | commit |
| `tests/test_claude_native.py` | `S21-claude-native-control` | `S10-inline-attachments` | path |
| `tests/test_claude_native_bridge.py` | `S21-claude-native-control` | `F28-alignment-residuals`, `S19-native-goal` | path |
| `tests/test_claude_native_compaction_recovery.py` | `S21-claude-native-control` |  | commit |
| `tests/test_claude_native_forwarder.py` | `S21-claude-native-control` |  | commit |
| `tests/test_claude_native_goal_recovery.py` | `S19-native-goal` |  | commit |
| `tests/test_claude_native_legacy_subagent_recovery.py` | `S21-claude-native-control` |  | commit |
| `tests/test_claude_native_status.py` | `S21-claude-native-control` |  | path |
| `tests/test_claude_native_status_file.py` | `S21-claude-native-control` |  | commit |
| `tests/test_claude_native_status_probe.py` | `S21-claude-native-control` |  | commit |
| `tests/test_claude_native_terminal_lifecycle.py` | `S21-claude-native-control` |  | commit |
| `tests/test_codex_native.py` | `S19-native-goal` |  | commit |
| `tests/test_codex_native_app_server.py` | `S20-codex-child-inventory` |  | path |
| `tests/test_codex_native_forwarder.py` | `S20-codex-child-inventory` |  | path |
| `tests/test_codex_rate_limits.py` | `S25-agent-library` |  | commit |
| `tests/test_native_forwarder_binding.py` | `S21-claude-native-control` |  | path |
| `tests/test_native_subagent_snapshots.py` | `S21-claude-native-control` |  | path |
| `tests/test_provider_usage_limits.py` | `S24-usage-context` |  | path |
| `web/electron/README.md` | `S26-archive-library` |  | commit |
| `web/electron/src/deepLink.js` | `S26-archive-library` |  | commit |
| `web/electron/test/deepLink.test.js` | `S26-archive-library` |  | commit |
| `web/ios/Omnigent/DeepLink.swift` | `S26-archive-library` |  | commit |
| `web/ios/OmnigentTests/DeepLinkTests.swift` | `S26-archive-library` |  | commit |
| `web/src/App.tsx` | `S26-archive-library` |  | commit |
| `web/src/components/AgentBadge.test.tsx` | `S25-agent-library` |  | commit |
| `web/src/components/AgentBadge.tsx` | `S25-agent-library` |  | commit |
| `web/src/components/AgentBadgeEditor.test.tsx` | `S25-agent-library` |  | commit |
| `web/src/components/AgentBadgeEditor.tsx` | `S25-agent-library` |  | commit |
| `web/src/components/AgentsSettings.test.tsx` | `S25-agent-library` |  | commit |
| `web/src/components/AgentsSettings.tsx` | `S25-agent-library` |  | commit |
| `web/src/components/CliRetentionSettings.test.tsx` | `S23-host-cli-retention` |  | commit |
| `web/src/components/CliRetentionSettings.tsx` | `S23-host-cli-retention` |  | commit |
| `web/src/components/ComposerMicButton.test.tsx` | `S09-dictation-punctuation` |  | commit |
| `web/src/components/ComposerMicButton.tsx` | `S09-dictation-punctuation` |  | commit |
| `web/src/components/ContextUsageSettings.test.tsx` | `S24-usage-context` |  | path |
| `web/src/components/ContextUsageSettings.tsx` | `S24-usage-context` |  | path |
| `web/src/components/InlineComposerEditor.test.tsx` | `S26-archive-library` | `S10-inline-attachments` | last-commit |
| `web/src/components/InlineComposerEditor.tsx` | `S26-archive-library` | `S10-inline-attachments`, `S11-configurable-hotkeys` | last-commit |
| `web/src/components/KeyboardShortcut.tsx` | `S11-configurable-hotkeys` |  | path |
| `web/src/components/KeyboardShortcutEditor.test.tsx` | `S11-configurable-hotkeys` |  | path |
| `web/src/components/KeyboardShortcutEditor.tsx` | `S11-configurable-hotkeys` |  | path |
| `web/src/components/MobileAssistantSettings.test.tsx` | `S13-mobile-assistant` |  | path |
| `web/src/components/MobileAssistantSettings.tsx` | `S13-mobile-assistant` |  | path |
| `web/src/components/MobileFloatingAssistant.test.tsx` | `S13-mobile-assistant` |  | path |
| `web/src/components/MobileFloatingAssistant.tsx` | `S13-mobile-assistant` |  | path |
| `web/src/components/SessionNavigationSettings.test.tsx` | `S21-claude-native-control` | `S19-native-goal` | last-commit |
| `web/src/components/SessionNavigationSettings.tsx` | `S21-claude-native-control` | `S19-native-goal` | last-commit |
| `web/src/components/SessionStateBadge.test.tsx` | `S21-claude-native-control` | `S19-native-goal` | last-commit |
| `web/src/components/SessionStateBadge.tsx` | `S21-claude-native-control` | `S19-native-goal` | last-commit |
| `web/src/components/archive/ArchiveDateRangePicker.test.tsx` | `S26-archive-library` |  | commit |
| `web/src/components/archive/ArchiveDateRangePicker.tsx` | `S26-archive-library` |  | commit |
| `web/src/components/archive/ArchiveLibraryRail.test.tsx` | `S26-archive-library` |  | commit |
| `web/src/components/archive/ArchiveLibraryRail.tsx` | `S26-archive-library` |  | commit |
| `web/src/components/archive/ArchiveLibraryToolbar.test.ts` | `S26-archive-library` |  | commit |
| `web/src/components/archive/ArchiveLibraryToolbar.tsx` | `S26-archive-library` |  | commit |
| `web/src/components/archive/ArchiveTranscriptViewer.stories.tsx` | `S26-archive-library` |  | path |
| `web/src/components/archive/ArchiveTranscriptViewer.test.tsx` | `S26-archive-library` |  | commit |
| `web/src/components/archive/ArchiveTranscriptViewer.tsx` | `S26-archive-library` |  | commit |
| `web/src/components/blocks/BlockRenderer.test.tsx` | `S02-assistant-linebreaks` |  | commit |
| `web/src/components/blocks/BlockRenderer.tsx` | `S02-assistant-linebreaks` |  | commit |
| `web/src/components/blocks/ChatMarkdown.tsx` | `S02-assistant-linebreaks` |  | commit |
| `web/src/components/blocks/TerminalImeInput.ts` | `S01-mobile-terminal-ime` |  | path |
| `web/src/components/blocks/TerminalSession.test.ts` | `S21-claude-native-control` |  | commit |
| `web/src/components/blocks/TerminalSession.ts` | `S21-claude-native-control` |  | commit |
| `web/src/components/blocks/TerminalView.test.tsx` | `S12-tui-softkeys-touch` |  | diff-read |
| `web/src/components/blocks/TerminalView.tsx` | `S12-tui-softkeys-touch` |  | diff-read |
| `web/src/components/blocks/terminalTextareaEdit.test.ts` | `S01-mobile-terminal-ime` |  | path |
| `web/src/components/blocks/terminalTextareaEdit.ts` | `S01-mobile-terminal-ime` |  | path |
| `web/src/components/chat/Transcript.tsx` | `S14-navigation-titles` |  | diff-read |
| `web/src/components/chat/chatBubbleParts.tsx` | `S26-archive-library` | `S10-inline-attachments` | last-commit |
| `web/src/components/scheduled/CreateScheduledTaskDialog.test.tsx` | `S07-host-roots-picker` |  | commit |
| `web/src/components/scheduled/CreateScheduledTaskDialog.tsx` | `S07-host-roots-picker` |  | commit |
| `web/src/embed.tsx` | `S17-ui-font-scale` |  | diff-read |
| `web/src/hooks/useAgentBadgePreferences.ts` | `S25-agent-library` |  | commit |
| `web/src/hooks/useApproveHotkey.ts` | `S11-configurable-hotkeys` |  | path |
| `web/src/hooks/useAvailableAgents.ts` | `S25-agent-library` |  | commit |
| `web/src/hooks/useChildSessions.ts` | `S21-claude-native-control` |  | commit |
| `web/src/hooks/useCommandPaletteHotkey.test.tsx` | `S11-configurable-hotkeys` |  | path |
| `web/src/hooks/useCommandPaletteHotkey.ts` | `S11-configurable-hotkeys` |  | path |
| `web/src/hooks/useContextIndicatorMode.ts` | `S24-usage-context` |  | path |
| `web/src/hooks/useConversations.test.ts` | `S25-agent-library` | `S23-host-cli-retention`, `S26-archive-library` | last-commit |
| `web/src/hooks/useConversations.ts` | `S21-claude-native-control` | `S19-native-goal`, `S23-host-cli-retention`, `S25-agent-library`, `S26-archive-library` | last-commit |
| `web/src/hooks/useFileDropTarget.test.tsx` | `S10-inline-attachments` |  | commit |
| `web/src/hooks/useFileDropTarget.ts` | `S10-inline-attachments` |  | commit |
| `web/src/hooks/useHostFilesystem.test.ts` | `S07-host-roots-picker` | `S03-windows-host-fixes` | last-commit |
| `web/src/hooks/useHostFilesystem.ts` | `S03-windows-host-fixes` | `S07-host-roots-picker` | last-commit |
| `web/src/hooks/useHosts.test.tsx` | `S23-host-cli-retention` | `S25-agent-library` | last-commit |
| `web/src/hooks/useHosts.ts` | `S07-host-roots-picker` | `S23-host-cli-retention`, `S25-agent-library` | last-commit |
| `web/src/hooks/useIdleNotifications.test.tsx` | `S21-claude-native-control` |  | commit |
| `web/src/hooks/useIdleNotifications.ts` | `S21-claude-native-control` |  | commit |
| `web/src/hooks/useMentionBrowser.ts` | `S10-inline-attachments` | `S11-configurable-hotkeys` | last-commit |
| `web/src/hooks/useNativeServerSwitcher.test.ts` | `S14-navigation-titles` |  | path |
| `web/src/hooks/useNativeServerSwitcher.ts` | `S14-navigation-titles` |  | path |
| `web/src/hooks/useNewSessionHotkey.test.tsx` | `S11-configurable-hotkeys` |  | path |
| `web/src/hooks/useNewSessionHotkey.ts` | `S11-configurable-hotkeys` |  | path |
| `web/src/hooks/useNewSessionTarget.test.tsx` | `S08-project-target-cwd` |  | path |
| `web/src/hooks/useNewSessionTarget.ts` | `S08-project-target-cwd` |  | path |
| `web/src/hooks/usePinnedSessionHotkeys.ts` | `S11-configurable-hotkeys` |  | path |
| `web/src/hooks/useResizableColumn.ts` | `S26-archive-library` |  | commit |
| `web/src/hooks/useResizableInlinePanel.test.tsx` | `S16-device-layout-memory` |  | commit |
| `web/src/hooks/useResizableInlinePanel.ts` | `S16-device-layout-memory` |  | commit |
| `web/src/hooks/useSessionNavigationPreferences.ts` | `S14-navigation-titles` |  | path |
| `web/src/hooks/useSessionPollingHotkeys.test.tsx` | `S11-configurable-hotkeys` | `S19-native-goal`, `S21-claude-native-control` | path |
| `web/src/hooks/useSessionPollingHotkeys.ts` | `S11-configurable-hotkeys` | `S19-native-goal`, `S21-claude-native-control` | path |
| `web/src/hooks/useSessionState.test.ts` | `S21-claude-native-control` |  | commit |
| `web/src/hooks/useSessionState.ts` | `S21-claude-native-control` |  | commit |
| `web/src/hooks/useSessionSwitchHotkey.ts` | `S11-configurable-hotkeys` |  | path |
| `web/src/hooks/useSidebarToggleHotkeys.ts` | `S11-configurable-hotkeys` |  | path |
| `web/src/hooks/useStableProviderUsageLimits.ts` | `S24-usage-context` |  | path |
| `web/src/hooks/useUnseenConversations.test.ts` | `S14-navigation-titles` |  | path |
| `web/src/hooks/useUnseenConversations.ts` | `S14-navigation-titles` |  | path |
| `web/src/hooks/useUsageContextPreferences.ts` | `S24-usage-context` |  | path |
| `web/src/hooks/useVoiceDictationHotkey.ts` | `S09-dictation-punctuation` |  | path |
| `web/src/index.css` | `S19-native-goal` |  | commit |
| `web/src/index.css.test.ts` | `S03-windows-host-fixes` |  | diff-read |
| `web/src/lib/agentBadgePreferences.test.ts` | `S25-agent-library` |  | commit |
| `web/src/lib/agentBadgePreferences.ts` | `S25-agent-library` |  | commit |
| `web/src/lib/agentGrouping.test.ts` | `S25-agent-library` |  | commit |
| `web/src/lib/agentGrouping.ts` | `S25-agent-library` |  | commit |
| `web/src/lib/archiveDateRange.ts` | `S26-archive-library` |  | commit |
| `web/src/lib/bootCapabilities.ts` | `S09-dictation-punctuation` |  | commit |
| `web/src/lib/capabilities.test.ts` | `S09-dictation-punctuation` |  | commit |
| `web/src/lib/capabilities.ts` | `S09-dictation-punctuation` |  | commit |
| `web/src/lib/composerContent.test.ts` | `S10-inline-attachments` |  | commit |
| `web/src/lib/composerContent.ts` | `S10-inline-attachments` |  | commit |
| `web/src/lib/composerSendShortcutPreferences.test.ts` | `S11-configurable-hotkeys` |  | path |
| `web/src/lib/composerSendShortcutPreferences.ts` | `S11-configurable-hotkeys` |  | path |
| `web/src/lib/contextIndicatorPreferences.test.ts` | `S24-usage-context` |  | path |
| `web/src/lib/contextIndicatorPreferences.ts` | `S24-usage-context` |  | path |
| `web/src/lib/customAgentsApi.test.ts` | `S25-agent-library` |  | commit |
| `web/src/lib/customAgentsApi.ts` | `S25-agent-library` |  | commit |
| `web/src/lib/dictation.test.ts` | `S09-dictation-punctuation` |  | commit |
| `web/src/lib/dictation.ts` | `S09-dictation-punctuation` |  | commit |
| `web/src/lib/events.ts` | `S24-usage-context` |  | diff-read |
| `web/src/lib/formatCost.test.ts` | `S24-usage-context` |  | path |
| `web/src/lib/formatCost.ts` | `S24-usage-context` |  | path |
| `web/src/lib/host.ts` | `S06-preference-sync` |  | path |
| `web/src/lib/identity.test.ts` | `S06-preference-sync` |  | path |
| `web/src/lib/identity.ts` | `S06-preference-sync` |  | path |
| `web/src/lib/idleTransitions.test.ts` | `S21-claude-native-control` |  | commit |
| `web/src/lib/idleTransitions.ts` | `S21-claude-native-control` |  | commit |
| `web/src/lib/keyboardShortcutPreferences.test.ts` | `S11-configurable-hotkeys` |  | path |
| `web/src/lib/keyboardShortcutPreferences.ts` | `S11-configurable-hotkeys` |  | path |
| `web/src/lib/lastCreatedWorkspace.test.ts` | `S07-host-roots-picker` |  | path |
| `web/src/lib/lastCreatedWorkspace.ts` | `S07-host-roots-picker` |  | path |
| `web/src/lib/mobileAssistantPreferences.test.ts` | `S13-mobile-assistant` |  | path |
| `web/src/lib/mobileAssistantPreferences.ts` | `S13-mobile-assistant` |  | path |
| `web/src/lib/newSessionTarget.test.ts` | `S08-project-target-cwd` |  | path |
| `web/src/lib/newSessionTarget.ts` | `S08-project-target-cwd` |  | path |
| `web/src/lib/providerUsageLimits.test.ts` | `S25-agent-library` |  | commit |
| `web/src/lib/providerUsageLimits.ts` | `S25-agent-library` |  | commit |
| `web/src/lib/sessionEvents.test.ts` | `S24-usage-context` |  | diff-read |
| `web/src/lib/sessionLinks.test.ts` | `S26-archive-library` |  | commit |
| `web/src/lib/sessionLinks.ts` | `S26-archive-library` |  | commit |
| `web/src/lib/sessionNavigationPreferences.test.ts` | `S14-navigation-titles` | `S19-native-goal`, `S21-claude-native-control` | path |
| `web/src/lib/sessionNavigationPreferences.ts` | `S14-navigation-titles` | `S19-native-goal`, `S21-claude-native-control` | path |
| `web/src/lib/sessionWorkspaceState.test.ts` | `S26-archive-library` |  | commit |
| `web/src/lib/sessionWorkspaceState.ts` | `S26-archive-library` |  | commit |
| `web/src/lib/sessionsApi.test.ts` | `S26-archive-library` |  | commit |
| `web/src/lib/sessionsApi.ts` | `S25-agent-library` | `S26-archive-library` | last-commit |
| `web/src/lib/settingsPortability.test.ts` | `S25-agent-library` |  | commit |
| `web/src/lib/settingsPortability.ts` | `S25-agent-library` |  | commit |
| `web/src/lib/sse.ts` | `S24-usage-context` |  | diff-read |
| `web/src/lib/types.ts` | `S25-agent-library` |  | commit |
| `web/src/lib/uiFontPreferences.test.ts` | `S17-ui-font-scale` |  | path |
| `web/src/lib/uiFontPreferences.ts` | `S17-ui-font-scale` |  | path |
| `web/src/lib/usageContextPreferences.test.ts` | `S24-usage-context` |  | path |
| `web/src/lib/usageContextPreferences.ts` | `S24-usage-context` |  | path |
| `web/src/lib/userPreferencesSync.test.ts` | `S25-agent-library` |  | commit |
| `web/src/lib/userPreferencesSync.ts` | `S25-agent-library` |  | commit |
| `web/src/pages/ArchiveSessionPage.test.tsx` | `S26-archive-library` |  | commit |
| `web/src/pages/ArchiveSessionPage.tsx` | `S26-archive-library` |  | commit |
| `web/src/pages/ChatPage.composer.test.tsx` | `S10-inline-attachments` |  | commit |
| `web/src/pages/ChatPage.historyLoad.test.tsx` | `S14-navigation-titles` |  | diff-read |
| `web/src/pages/ChatPage.statusLine.test.tsx` | `S25-agent-library` |  | commit |
| `web/src/pages/ChatPage.test.ts` | `S10-inline-attachments` |  | commit |
| `web/src/pages/ChatPage.tsx` | `S10-inline-attachments` |  | commit |
| `web/src/pages/ChatPage.userBubble.test.tsx` | `S10-inline-attachments` |  | commit |
| `web/src/pages/SettingsPage.test.tsx` | `S26-archive-library` | `S23-host-cli-retention` | last-commit |
| `web/src/pages/SettingsPage.tsx` | `S21-claude-native-control` | `S23-host-cli-retention`, `S25-agent-library`, `S26-archive-library` | last-commit |
| `web/src/pages/TurnRail.tsx` | `S26-archive-library` |  | commit |
| `web/src/shell/AppShell.test.tsx` | `S26-archive-library` | `S19-native-goal` | last-commit |
| `web/src/shell/AppShell.tsx` | `S26-archive-library` | `S19-native-goal` | last-commit |
| `web/src/shell/BrowseLocationBar.tsx` | `S07-host-roots-picker` |  | commit |
| `web/src/shell/ChatHeader.stories.tsx` | `S26-archive-library` |  | commit |
| `web/src/shell/ChatHeader.test.tsx` | `S26-archive-library` |  | commit |
| `web/src/shell/ChatHeader.tsx` | `S26-archive-library` |  | commit |
| `web/src/shell/ChatPlanAccordion.test.tsx` | `S18-native-plan` |  | path |
| `web/src/shell/ChatPlanAccordion.tsx` | `S18-native-plan` |  | path |
| `web/src/shell/CommandPalette.test.tsx` | `S11-configurable-hotkeys` |  | path |
| `web/src/shell/CommandPalette.tsx` | `S11-configurable-hotkeys` |  | path |
| `web/src/shell/ConversationBreadcrumb.tsx` | `S14-navigation-titles` |  | path |
| `web/src/shell/CreateAgentDialog.test.tsx` | `S25-agent-library` |  | commit |
| `web/src/shell/CreateAgentDialog.tsx` | `S25-agent-library` |  | commit |
| `web/src/shell/FilesPanel.test.tsx` | `S07-host-roots-picker` |  | commit |
| `web/src/shell/ForkSessionDialog.test.tsx` | `S07-host-roots-picker` |  | commit |
| `web/src/shell/ForkSessionDialog.tsx` | `S07-host-roots-picker` |  | commit |
| `web/src/shell/HeaderConversationMenu.archive.integration.test.tsx` | `S26-archive-library` |  | path |
| `web/src/shell/HeaderConversationMenu.test.tsx` | `S26-archive-library` |  | diff-read |
| `web/src/shell/HeaderConversationMenu.tsx` | `S26-archive-library` |  | diff-read |
| `web/src/shell/NewChatDialog.flow.test.tsx` | `S08-project-target-cwd` | `S07-host-roots-picker`, `S10-inline-attachments`, `S25-agent-library` | path |
| `web/src/shell/NewChatDialog.projectCreate.test.tsx` | `S08-project-target-cwd` |  | path |
| `web/src/shell/NewChatDialog.projectPrefill.test.tsx` | `S07-host-roots-picker` |  | commit |
| `web/src/shell/NewChatDialog.test.tsx` | `S08-project-target-cwd` | `S07-host-roots-picker`, `S10-inline-attachments`, `S25-agent-library`, `S26-archive-library` | path |
| `web/src/shell/NewChatDialog.tsx` | `S08-project-target-cwd` | `S07-host-roots-picker`, `S10-inline-attachments`, `S11-configurable-hotkeys`, `S25-agent-library` | path |
| `web/src/shell/NewChatLandingScreen.mobileChrome.test.tsx` | `S07-host-roots-picker` |  | commit |
| `web/src/shell/ProjectSettingsDialog.test.tsx` | `S07-host-roots-picker` |  | commit |
| `web/src/shell/ProjectSettingsDialog.tsx` | `S07-host-roots-picker` |  | commit |
| `web/src/shell/ReconcileSubagentsButton.test.tsx` | `S21-claude-native-control` |  | diff-read |
| `web/src/shell/ReconcileSubagentsButton.tsx` | `S21-claude-native-control` |  | diff-read |
| `web/src/shell/ResumeWithDirectoryDialog.test.tsx` | `S07-host-roots-picker` |  | commit |
| `web/src/shell/ResumeWithDirectoryDialog.tsx` | `S07-host-roots-picker` |  | commit |
| `web/src/shell/Sidebar.newSession.stories.tsx` | `S08-project-target-cwd` |  | diff-read |
| `web/src/shell/Sidebar.projectContextMenu.test.tsx` | `S08-project-target-cwd` |  | path |
| `web/src/shell/Sidebar.projectHeaderChevron.test.tsx` | `S08-project-target-cwd` |  | path |
| `web/src/shell/Sidebar.rowActions.test.tsx` | `S15-global-read-all` | `S16-device-layout-memory`, `S19-native-goal` | path |
| `web/src/shell/Sidebar.stop.test.tsx` | `S23-host-cli-retention` |  | commit |
| `web/src/shell/Sidebar.test.tsx` | `S21-claude-native-control` | `S19-native-goal`, `S25-agent-library` | last-commit |
| `web/src/shell/Sidebar.tsx` | `S21-claude-native-control` | `S16-device-layout-memory`, `S19-native-goal`, `S23-host-cli-retention`, `S25-agent-library` | last-commit |
| `web/src/shell/SubagentsGraphView.test.tsx` | `S21-claude-native-control` |  | commit |
| `web/src/shell/SubagentsGraphView.tsx` | `S21-claude-native-control` |  | commit |
| `web/src/shell/SubagentsPanel.test.tsx` | `S21-claude-native-control` |  | commit |
| `web/src/shell/SubagentsPanel.tsx` | `S21-claude-native-control` |  | commit |
| `web/src/shell/SwitchHostDialog.test.tsx` | `S07-host-roots-picker` |  | commit |
| `web/src/shell/SwitchHostDialog.tsx` | `S07-host-roots-picker` |  | commit |
| `web/src/shell/WorkspacePanel.test.tsx` | `S26-archive-library` |  | commit |
| `web/src/shell/WorkspacePanel.tsx` | `S26-archive-library` |  | commit |
| `web/src/shell/WorkspacePicker.stories.tsx` | `S07-host-roots-picker` |  | commit |
| `web/src/shell/WorkspacePicker.test.tsx` | `S07-host-roots-picker` | `S03-windows-host-fixes` | path |
| `web/src/shell/WorkspacePicker.tsx` | `S07-host-roots-picker` | `S03-windows-host-fixes` | path |
| `web/src/shell/railTabs.ts` | `S26-archive-library` |  | commit |
| `web/src/shell/settingsNav.test.tsx` | `S13-mobile-assistant` |  | diff-read |
| `web/src/shell/settingsNav.tsx` | `S25-agent-library` | `S23-host-cli-retention` | last-commit |
| `web/src/shell/subagentStatus.nativeLiveness.test.ts` | `S21-claude-native-control` |  | path |
| `web/src/shell/subagentStatus.ts` | `S21-claude-native-control` |  | commit |
| `web/src/store/chatStore.test.ts` | `S21-claude-native-control` | `S10-inline-attachments`, `S19-native-goal` | last-commit |
| `web/src/store/chatStore.ts` | `S21-claude-native-control` | `S10-inline-attachments`, `S19-native-goal` | last-commit |
| `web/src/store/conversationState.ts` | `S24-usage-context` |  | diff-read |

## Narrative / Evidence (dated)

- [260912] **v1 -> v2 rebuild** (board OMN04 / task T260912-016). v1 held 3 rows while the branch diverged from `upstream/main` by 84 commits and 443 files. The 48-MOD ledger written on the Windows host was never committed: all seven worktree snapshots exported on 2026-09-12 (`imports/OMN04-ledger-export/`) hold only the 1-row or 2-row version, and the export's own verification records that the full ledger was not found. v2 is therefore derived, not recovered.
- [260912] **Method.** Paths came from `git diff --name-only <merge-base>...<tip>`; path-to-commit from `git log --reverse --diff-merges=first-parent --name-only`. The `--diff-merges=first-parent` part is load-bearing: 23 paths exist only as merge resolutions and a plain `git log` attributes them to nothing. Each path's primary owner was decided in four tiers — 24 by reading the hunk, 227 by single-feature commits, 134 by filename, 58 by most-recent intent on an aggregate file. 74 paths carry a co-owner. The derivation script and its full output are committed outside this repository, in the coordination root at `docs/task-files/26/09/T260912-016-evidence/`, so they can never reach an outbound PR branch.
- [260912] **Row count.** 30 active rows exceeds the template's "keep active row count manageable (<15)" guidance. This is deliberate: the fork genuinely carries 26 feature groups plus three maintenance buckets and the ledger itself, and merging unrelated groups to reach 15 would destroy the per-feature PR mapping this registry exists to serve. Do not consolidate rows to hit the number.
- [260912] **v1 row mapping.** Row 1 -> `S04-askuserquestion-wait`, row 2 -> `S21-claude-native-control`, row 3 -> `S23-host-cli-retention`. All three keep their original `why` and exit condition verbatim. Row 1's anchor was corrected: `omnigent/claude_native_bridge.py` exists on neither `local/host-custom` nor `upstream/main`, and the mod now lives at `omnigent/harnesses/claude_native/bridge.py:2009` — an upstream file move, not a new modification.
- [260912] **Correction.** An earlier draft of this rebuild attributed `omnigent/runner/background_titles/service.py` to the merged upstream background-title feature. That was wrong: its residual is the `release(...) -> bool | None` return-type change from `afb2fd534`, an event-type alignment. The row is `F28-alignment-residuals` and claims no upstream PR.
- [260912] **Blind spots.** (1) The 58 `last-commit` paths are aggregate files — `openapi.json`, `omnigent/db/db_models.py`, `omnigent/server/schemas.py`, `omnigent/stores/conversation_store/*`, `web/src/shell/Sidebar.tsx` — whose residual genuinely belongs to several rows; their `primary` is a hint and their `also` column is the truthful statement. (2) Every `why` is derived from FEATURE_MAP, commit subjects and read hunks, never from the original author. (3) `lifecycle` values marked as decisions in the rebuild have not been confirmed against live upstream state; re-verify before acting on one. (4) The branch moved three times during the rebuild (`8468d526e` -> `19a4aa67b` -> `f72a14fd7` -> `5ab623360`) from parallel work on the archive-close coordinator, while the path count stayed at 443 — a stable path count is not evidence of a stable tip.

## Upgrade Checklist

This registry drives the `upstream-update` skill (Phase B3/C/F/J/L):

1. Each Committed Modifications row becomes one triage input: (paths, area, what, why, canonical_base).
2. The bootstrap upstream hash above is the canonical_base for rows with no single-feature commit.
3. Verdict ADOPT / absorption=absorbed -> the row moves to Previously Applied.
4. Verdict KEEP_NOTE -> a divergence note is appended to Narrative.
5. The Contribution Index is reconciled from verified GitHub state each run (Phase F); merged branches are GC'd (Phase J).

**Maintenance guidance**: keep the Path Ownership table regenerated whenever the branch is re-aligned — it is the ledger's own proof that no residual is unowned. Periodically PR upstream so rows retire; a stale `why` is marked for removal on the next run.
