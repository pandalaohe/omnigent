# Terminal key bars

Use the [terminal recipe](../../../feature-map/terminals.md) for a real terminal
and the [Verify Omnigent skill](../../../feature-map/skills/verify-omnigent/SKILL.md)
for an isolated instance. The browser-contract lane serves the built SPA and
models the terminal WebSocket; it proves browser input without starting a runner.

## Automated checks

Build with `pnpm --filter web run build`, then run
`tests/browser_ui/chat/test_mobile_assistant_input.py --browser-ui-skip-build`
through the checkout's required pytest isolation wrapper. These scenarios cover
the floating fallback, independently paged rows, pinned keys, page target geometry,
text payloads and one-shot Shift/Ctrl navigation bytes.

Run the related web component, preferences, sync and terminal-session tests with
the project's supported Node version. The shared UTF-8 boundary fixtures at
`tests/fixtures/terminal_bar_preference_sizes.json` are checked against the server
by `tests/server/test_user_preferences.py -k terminal_bar`.

## Human interaction

In Settings → Mobile controls → Terminal, arrange a row using a drag handle.
Change a key to one cell, edit its label and function, save it, and verify the
terminal bar reflects that edit. Pin the first key, add enough keys to page, and
confirm that key stays while the row's own page button advances. The left page
button belongs to the top row; the right belongs to the bottom row.

Tap Ctrl, then an arrow: Ctrl returns to off. Hold Ctrl for at least 500 ms,
release, then press several arrows: Ctrl stays locked until tapped. Turn Repeat
on for an arrow and hold it: sends start after 400 ms. Switch away from the tab
or reconnect while a modifier is armed: modifiers return to off.

Save a preset, edit the layout, load the preset, restore defaults, then Undo.
Disable the bottom row and enable it again: its keys remain. Repeat the layout
checks in portrait and landscape. Check touch drag and keyboard reorder separately.

Record the build identity, viewport, input and observed results. A component
acceptance page uses a simulated session and does not prove server synchronization
or a physical phone's IME. Preserve failed traces; close only instances and
browser contexts created for this run.
