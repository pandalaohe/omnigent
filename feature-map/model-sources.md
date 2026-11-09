# Model lists on a host

The Model list card in Settings shows where an agent's model list comes from,
how many models it contains, and a Refresh action. Connect your own offers the
same setup rules as `omnigent models guide`. A host owner can declare a URL,
command, static list or ACP agent in the global configuration; this supplies a
list without changing the agent's provider connection.

## Sub-features

- `list`: source and count, including loading, empty, failed and offline states.
- `refresh`: refresh now, with the previous nonempty list retained on empty or
  failed answers; a stale ordinary read serves first and refreshes in the background.
- `connect`: read and copy setup rules, with copy success or failure feedback.
- `check`: offline CLI checks with structured results and exit codes 0, 1 and 2.
- `probe`: ACP initialization and temporary session creation, without a prompt,
  authentication flow or MCP relay, followed by process cleanup.
- `ACP session controls`: New Chat uses the agent's model list or text entry;
  a running chat shows its reported model and offers the agent's selectable modes.
- `start thinking`: agents may offer thinking levels that apply when a chat starts;
  those levels appear before creation and do not add a running thinking control.

## How to get to it (user POV)

- Open Settings → Agents, open an agent and select its host. Read Model list,
  press Refresh or open Connect your own.
- On the host, run `omnigent models guide` or
  `omnigent models check <harness> --json`. Omitting the harness checks known
  built-ins and configured sources. Neither command starts a server or daemon.
- Use an available model menu or the Settings defaults table after configuring
  the source. An empty list permits a model name where that surface supports
  text entry. The listing does not itself enable model switching on an agent.
- In New Chat, select the configured ACP agent on its host, then pick a model,
  thinking level and mode where offered. In a running chat, open the model or
  permission control. Pi RPC uses its host's model dropdown in the running chat.

## Driving it with the repro environment

Preconditions: use the checkout's isolated test environment and synthetic
configuration. Never point a verification run at the operator's home directory.

- `tests/test_model_sources.py` exercises configuration errors, URL and command
  listings, refresh and failure preservation, CLI JSON and packaged rules.
- `tests/inner/test_acp_model_probe.py` starts a real neutral stdio peer and
  checks advertisements, refusal, exit, timeout, request inventory and cleanup.
- `tests/host/test_acp_model_options.py` exercises the host model-options entry.
- `tests/server/routes/test_calling_defaults_routes.py` exercises cached mode
  sync, resolution, creation, offline changes, fork/continue and model reports.
  Its scripted ACP journeys cross the real server, runner, harness and stdio
  agent for both config-option and legacy mode selection.
- `tests/inner/test_acp_executor.py` and `tests/inner/test_acp_effort.py` cover
  model refusal notices, actual-model reports, mode confirmation and start thinking.
- `web/src/pages/settings/AgentModelList.test.tsx` drives the production card's
  refresh, failed refresh, empty state, rule copy and host changes.
- In the isolated app, open a configured agent's Model list, verify its source
  and count, press Refresh and expect Refreshed just now. Open Connect your own,
  copy the rules, then repeat at 390 px and in both themes. No settings write
  should occur from these actions.
- To test the CLI manually, add `model_sources: {copilot: {static: [model-a]}}`
  to an isolated global config and run `omnigent models check copilot --json`.
  Expect a count of 1. Change the list to `[]` and expect an empty result with
  exit 0. A malformed YAML document exits 2; a failed listing exits 1.
- With an isolated scripted ACP agent, press Sync models in Settings → Agents.
  Open New Chat and check that its active model is the agent default, that an
  empty list allows text entry, and that a start-only thinking level says
  Applies when a chat starts. Start a chat, send one message, and check the model
  shown agrees with the agent's report. Change the model and mode, then send
  another message: each setting must reach the same agent session before its
  next prompt. A rejected setting prints Agent kept followed by the retained
  setting. Repeat with zero or one model row, Pi RPC, both themes and 390 px.

## Gotchas

- Run commands on the host that launches the agent. Localhost in a URL names
  that host, rather than the browser or Omnigent server.
- A declared source takes precedence over the built-in listing. The web does
  not edit `model_sources`; a command in it is trusted host-owner code.
- `pick` reports whether a selection can reach the agent. A menu alone is not
  proof of a working provider connection; follow the check's hint.
- Environment references name credentials and never print their values. Restart
  the host daemon after adding an environment reference.
- ACP probing defaults to 60 seconds including cleanup and creates no model prompt.
  A timeout identifies the stage and budget; failures point to a private local
  stderr diagnostic, replaced by the next failure. New Chat and Settings
  requests have shorter HTTP or browser deadlines and can time out before the
  probe finishes.
- ACP mode names have agent-defined meanings. An unclassified mode is selectable
  by its label; totals skip it and it is not marked Full access. Only the host
  owner's `permission_concepts` mapping assigns a shared meaning. This changes
  the agent's own mode, independently of Omnigent's tool approval stance.
- An older host that has not reported modes cannot accept an explicit ACP mode;
  the error directs the owner to Sync models. A running chat can save a mode
  while its runner is offline; its next initialization carries the saved choice.
- Token-free model reports update the model label without adding a zero-token
  usage row. A curated model shortlist keeps its labels and ordering when a
  running agent advertises a wider catalog.
