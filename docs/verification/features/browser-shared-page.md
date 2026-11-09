# Browser shared page status and Stop

## Component checks

Use the checkout's own web dependencies and Node 24. From `web/`, run:

```sh
pnpm exec vitest run src/hooks/useBrowserAgentRelay.test.tsx src/hooks/useBrowserTabs.test.tsx src/lib/browserActionBus.test.ts src/components/BrowserPane/BrowserActivityStatus.test.tsx src/components/BrowserPane/BrowserPane.test.tsx src/store/chatStore.test.ts
pnpm exec tsc -b
```

These checks cover per-view activity, overlapping actions, remounts, the quiet
period, named-tab claims, owner-targeted interrupt and chat interrupt failures.

## Desktop trial

Use the isolated instance recipe linked from the verification index. Open a
session's browser tab and ask the session to inspect or use that page. While a
tool runs, expect “Session is operating this page” and Stop above the page;
continue typing in the page. After the last tool finishes, expect the line to
disappear after about 2.5 seconds. Switch browser tabs during a longer tool:
the line belongs to its target tab and survives switching away and back.

Click Stop during a tool. Expect the same interrupt result or failure in chat
as the chat Stop control, with the session process retained. Send another
message to confirm the session still responds. With two desktop windows, send
a tool with an explicit `tab_id`: the window containing its native view must
handle it. A window with only saved tab metadata must leave the claim alone.

Record checkout revision, desktop version, action, observed status, interrupt
outcome and screenshots. A component preview with a simulated interrupt
receiver does not verify the real runner or native page bounds. Close only the
isolated instance and pages created for this trial, following its recipe.
