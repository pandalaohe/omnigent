Connect models to Omnigent. Rules for the agent doing the setup.

Done means: `omnigent models check <harness> --json` exits 0 and shows
`status: ok`, `pick: reaches` for that harness, or you told the user why not (rule 6).
File: ~/.omnigent/config.yaml. Only this global file is read. Keep every other key.

1. Run `omnigent models check --json` first. A harness already ok needs no change.
2. Connect the agent before listing models. A model_sources line fills the menu only.
   - Pi, Claude, Codex through a gateway or key: add a `providers:` entry. Add
     `inference.harnesses.<harness>` when the ids are not that agent's own family.
   - Custom ACP agent: add it under `acp.agents`. If it cannot switch models when
     asked, put the model in its `command`, or set `send_model: true`.
   - Anything else, such as a proxy with its own header, Azure, or Bedrock on a
     non-Claude agent: use that agent's own config.
3. Only if the agent reports no list, add one line under `model_sources`, keyed by
   harness id, with exactly one of:
   - url: an OpenAI-compatible base, or a full URL ending in /models.
     Add api_key_ref only if the server needs a key.
   - command: prints a JSON list of ids, or `id<TAB>label` lines. Use it for any
     other auth header, filtering or renaming. On Windows, point it at a script
     file and print UTF-8.
   - static: a list of ids. Azure ids are deployment names.
   - agent: true: ask the agent. The default for ACP; may be omitted.
4. Never write a key in this file. api_key_ref names where it lives. Prefer
   keychain:<name>. env:<NAME> must also reach the host daemon; check reports it.
5. Run `omnigent models check <harness> --json`. Fix what it names and repeat.
   Exit 0: no source failed. 1: a source failed. 2: bad config or usage.
6. Read `pick`. reaches: done. needs-binding: go back to rule 2. list-only or
   refused: tell the user the menu shows these models but choosing one will not
   change the agent's model. Do not hide this.
7. Tell the user to press Refresh in the model menu, and to restart the host
   daemon if rule 4 added an env: name.

```yaml
model_sources:
  acp:local-agent:   {url: "http://localhost:11434/v1"}   # Ollama, no key; localhost means the host
  acp:gateway-agent: {url: "https://llm.example.com/v1", api_key_ref: "keychain:llm"}   # LiteLLM, OpenRouter
  acp:gemini-agent:  {url: "https://generativelanguage.googleapis.com/v1beta/openai/models", api_key_ref: "keychain:gemini"}
  acp:proxy-agent:   {command: "/opt/scripts/list-models.py"}   # own header, filter, rename, Bedrock, Vertex
  copilot:           {static: [model-a, model-b]}   # no list anywhere; Azure deployment names
  acp:reporting:     {agent: true}   # the agent reports its own; may be omitted
```
