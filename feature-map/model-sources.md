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

## How to get to it (user POV)

- Open Settings → Agents, open an agent and select its host. Read Model list,
  press Refresh or open Connect your own.
- On the host, run `omnigent models guide` or
  `omnigent models check <harness> --json`. Omitting the harness checks known
  built-ins and configured sources. Neither command starts a server or daemon.
- Use an available model menu or the Settings defaults table after configuring
  the source. An empty list permits a model name where that surface supports
  text entry. The listing does not itself enable model switching on an agent.

## Driving it with the repro environment

Preconditions: use the checkout's isolated test environment and synthetic
configuration. Never point a verification run at the operator's home directory.

- `tests/test_model_sources.py` exercises configuration errors, URL and command
  listings, refresh and failure preservation, CLI JSON and packaged rules.
- `tests/inner/test_acp_model_probe.py` starts a real neutral stdio peer and
  checks advertisements, refusal, exit, timeout, request inventory and cleanup.
- `tests/host/test_acp_model_options.py` exercises the host model-options entry.
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

## Gotchas

- Run commands on the host that launches the agent. Localhost in a URL names
  that host, rather than the browser or Omnigent server.
- A declared source takes precedence over the built-in listing. The web does
  not edit `model_sources`; a command in it is trusted host-owner code.
- `pick` reports whether a selection can reach the agent. A menu alone is not
  proof of a working provider connection; follow the check's hint.
- Environment references name credentials and never print their values. Restart
  the host daemon after adding an environment reference.
- ACP probing is bounded to 30 seconds and creates no model prompt. Ordinary
  New Chat picker requests keep their shorter request budget; explicit Settings
  model-list requests allow the probe to finish.
