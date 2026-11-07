# Starting prompts

New Chat can add text for one session. SDK, RPC and built-in team agents can
save an owner-specific starting prompt and restore the shipped text. Agents
that build their own prompt explain why Startup prompts are not sent.

## Sub-features

- `session-text`: optional text follows shared and role instructions, persists
  with the session, and carries onto a fork. The limit is 16 KiB in UTF-8.
- `builtin-version`: edit a starting prompt, save an empty or nonempty version,
  restore the built-in, and Undo restoration. Edits belong to the signed-in user.
- `builtin-drift`: a changed built-in shows a notice beside Restore built-in.
- `reconnect`: new chats use saved text; running chats adopt it on reconnect.
- `not-sent`: host ACP agents whose prompt injection is off show a help hint.
  Session creation still records optional text and returns a warning.

## How to get to it (user POV)

- New Chat → Prompt for this chat.
- Settings → Agents → SDK or RPC agent → Prompt → Starting prompt.
- Settings → Agents → Team agents → View → Starting prompt.
- Settings → Agents → Prompt table, and Settings → Startup prompts, show the
  host-specific Not sent label. New Chat and the agent's Prompt card show it too.

## Driving it with the repro environment

Preconditions: a signed-in owner; a seeded SDK/RPC or team agent. For the ACP
label, the selected host reports an ACP agent with prompt injection disabled.
Opening the summary fetches missing delivery metadata, even if its model probe
fails. The host's ordinary model-list refresh updates this metadata too.

1. Open New Chat, expand Prompt for this chat, enter a short instruction, then
   create the session with a first message. Fork it and check that the same
   startup text is retained. The REST session snapshot exposes `session_prompt`.
2. Open a supported agent's Starting prompt, save different text, and start a
   chat. A running chat retains its prior snapshot until reconnect. A second
   signed-in user still sees the built-in text. After reconnect, send another
   message: a live Claude SDK client adopts the new text and retains history.
3. Restore built-in, then Undo. The text and saved/restored status follow the
   actions. Shared and host layers remain read-only on this page, with the
   Edit in Startup prompts link.
4. Select the ACP agent with injection disabled. The four surfaces show
   Not sent · builds its own prompt; open its question mark for the explanation.

Component checks: `BuiltinPromptEditor.test.tsx`, `AgentContextTable.test.tsx`,
`InstructionsPage.test.tsx`, `NewChatDialog.s10.test.tsx` in the web test runner.
Server and runner checks: `test_builtin_agent_prompts.py`,
`test_instruction_layers.py`, `test_runner_session_init.py`, and the prompt
override scenario in `test_app_sessions_native_workflow_init.py`.

## Gotchas

- Optional startup text is separate from the first user message.
- Native CLI agents keep their own base prompt; shared text is edited in
  Startup prompts. Their vendor prompt is not editable on the Agent page.
- The built-in editor changes a user preference, leaving the shared bundle
  unchanged. A fork follows its current owner's preference on initialization.
- A fake-data HTML preview proves controls and layout, not a live provider.
