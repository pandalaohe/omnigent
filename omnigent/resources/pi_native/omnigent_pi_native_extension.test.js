// Unit test for the pi-native bridge extension's interrupt / replay logic.
//
// Regression coverage for F18 (SDK_INTEGRATION_BUG_AUDIT.md): an interrupt that
// arrives while Pi is idle used to arm a 30s replay window that aborted the next
// legitimately-started turn. ExtensionContext.abort() is a silent no-op when the
// agent is idle (it does not throw), so the old requestInterrupt() armed the
// window unconditionally and replayPendingInterrupt() then killed the next turn.
//
// This test drives the real extension through its public surface: it registers
// the event handlers with a mock `pi`, and delivers interrupts through the real
// inbox poller (a temp inbox directory). HTTP calls use the mock fetch below.
//
// Run with: node omnigent/resources/pi_native/omnigent_pi_native_extension.test.js
//
// Manual reproduction of the original bug (for context):
//   1. Start a native Pi session linked to Omnigent and let it go idle.
//   2. Hit "stop"/interrupt while no turn is running (between turns).
//   3. Send a fresh user message within 30 seconds.
//   Before the fix: the fresh turn is aborted immediately at agent_start /
//   turn_start (and tool calls are blocked) before producing output. After the
//   fix: the idle interrupt is dropped and the fresh turn runs normally.

const fs = require("fs");
const os = require("os");
const path = require("path");

const EXT_PATH = path.resolve(__dirname, "omnigent_pi_native_extension.js");

const harnesses = [];

// Build a fresh extension instance with its own temp inbox directory. Each call
// produces independent closure state (activeResponseId, pendingInterruptUntil,
// latestContext, ...).
function makeHarness({ captureEvents = false, existingTools = [], startupInstructions } = {}) {
  const bridgeDir = fs.mkdtempSync(path.join(os.tmpdir(), "pi-native-inbox-"));
  const inboxDir = path.join(bridgeDir, "inbox");
  fs.mkdirSync(inboxDir);
  const configPath = path.join(bridgeDir, "config.json");
  const config = {
    inboxDir, bridgeDir, serverUrl: "http://mock", sessionId: "conv_test",
  };
  fs.writeFileSync(path.join(bridgeDir, "permission_state.json"), JSON.stringify({
    session_id: config.sessionId, host_id: "test-host", workspace: bridgeDir,
    tier: "ask", revision: 1, status: "ready",
  }));
  if (startupInstructions !== undefined) config.startupInstructions = startupInstructions;
  const fixtureRoot = bridgeDir;
  fs.writeFileSync(configPath, JSON.stringify(config));
  process.env.OMNIGENT_PI_NATIVE_CONFIG = configPath;

  // Capture posted event bodies (status edges etc.) instead of hitting network.
  const postedEvents = [];
  global.fetch = async (url, opts) => {
      try {
        if (captureEvents && opts && typeof opts.body === "string" && String(url).endsWith("/events")) {
          postedEvents.push(JSON.parse(opts.body));
        }
      } catch (_err) {}
      return { ok: true, status: 200, json: async () => ({ result: "POLICY_ACTION_ALLOW" }) };
    };

  const handlers = {};
  const registeredTools = {};
  const pi = {
    on: (name, fn) => {
      handlers[name] = fn;
    },
    registerCommand: () => {},
    registerTool: (tool) => {
      registeredTools[tool.name] = tool;
    },
    getAllTools: () => existingTools,
    sendUserMessage: () => {},
  };

  // Fresh module-function invocation -> fresh closures.
  delete require.cache[EXT_PATH];
  const mod = require(EXT_PATH);
  mod(pi);

  const h = { pi, handlers, fixtureRoot, configPath, bridgeDir, inboxDir, postedEvents, registeredTools };
  harnesses.push(h);
  return h;
}

function statusEdges(postedEvents) {
  return postedEvents
    .filter((e) => e && e.type === "external_session_status" && e.data)
    .map((e) => ({ status: e.data.status, responseId: e.data.response_id }));
}

// ctx mock. `idle` may be true/false (exposes isIdle()) or undefined (no isIdle
// method at all, exercising the activeResponseId fallback path).
function makeCtx({ idle } = {}) {
  const ctx = {
    abortCount: 0,
    abort() {
      this.abortCount += 1;
    },
  };
  if (idle !== undefined) ctx.isIdle = () => idle;
  return ctx;
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

// Drop an interrupt into the inbox and wait until the poller has consumed it
// (the poller unlinks the file after invoking handleInterrupt -> requestInterrupt).
async function deliverInterrupt(h) {
  const file = path.join(h.inboxDir, `int-${Date.now()}-${Math.random().toString(36).slice(2)}.json`);
  fs.writeFileSync(file, JSON.stringify({ type: "interrupt" }));
  const deadline = Date.now() + 3000;
  while (fs.existsSync(file)) {
    if (Date.now() > deadline) throw new Error("interrupt file was not consumed by poller");
    await sleep(20);
  }
  // The poller runs requestInterrupt synchronously before unlinking, so by the
  // time the file is gone the interrupt has been processed.
}

function assert(name, cond, detail) {
  console.log(`${cond ? "PASS" : "FAIL"}  ${name}${detail ? "  -- " + detail : ""}`);
  if (!cond) process.exitCode = 1;
}

async function testStartupInstructionsReachEveryAgentStart() {
  const h = makeHarness({
    captureEvents: true,
    startupInstructions: "Author brief.\n\nGlobal directions.",
  });
  const base = "Pi native prompt\n\nAPPEND_SYSTEM content";
  const expected = `${base}\n\nAuthor brief.\n\nGlobal directions.`;
  const first = await h.handlers.before_agent_start({ systemPrompt: base }, {});
  await h.handlers.input({ text: "typed in terminal" }, {});
  await h.handlers.message_end(
    { message: { role: "user", content: [{ type: "text", text: "typed in terminal" }] } },
    {},
  );
  const second = await h.handlers.before_agent_start({ systemPrompt: base }, {});
  const repeated = await h.handlers.before_agent_start(
    { systemPrompt: first.systemPrompt }, {},
  );
  const compacted = await h.handlers.before_agent_start(
    { systemPrompt: "Pi prompt after compaction" }, {},
  );
  assert("first native prompt retains APPEND_SYSTEM and gets startup text", first.systemPrompt === expected);
  assert("second agent start gets the same startup text", second.systemPrompt === expected);
  assert(
    "terminal input stays a user message while the hook supplies instructions",
    h.postedEvents.some((event) => event.type === "external_conversation_item" &&
      event.data.item_data.content[0].text === "typed in terminal"),
  );
  assert("repeated hook does not duplicate startup text", repeated === undefined);
  assert(
    "post-compaction native prompt gets startup text",
    compacted.systemPrompt === "Pi prompt after compaction\n\nAuthor brief.\n\nGlobal directions.",
  );
  const empty = makeHarness({ startupInstructions: "   " });
  const absent = makeHarness();
  assert("blank instructions leave native prompt alone", !await empty.handlers.before_agent_start({ systemPrompt: base }, {}));
  assert("absent instructions leave native prompt alone", !await absent.handlers.before_agent_start({ systemPrompt: base }, {}));
}

async function testIdleInterruptDoesNotPoisonNextTurn() {
  const h = makeHarness();
  const idleCtx = makeCtx({ idle: true });
  await h.handlers.session_start({}, idleCtx);

  await deliverInterrupt(h);

  assert(
    "idle interrupt (isIdle) does not abort the idle context",
    idleCtx.abortCount === 0,
    `abortCount=${idleCtx.abortCount}`,
  );

  // A fresh, legitimate turn starts within the (old) 30s window.
  const turnCtx = makeCtx({ idle: false });
  await h.handlers.agent_start({}, turnCtx);
  await h.handlers.turn_start({ turnIndex: 1 }, turnCtx);
  const toolResult = await h.handlers.tool_call(
    { toolCallId: "t1", toolName: "do_thing", input: {} },
    turnCtx,
  );

  assert(
    "fresh turn after idle interrupt is NOT aborted",
    turnCtx.abortCount === 0,
    `abortCount=${turnCtx.abortCount}`,
  );
  assert(
    "fresh turn's tool_call is NOT blocked after idle interrupt",
    !toolResult || toolResult.block !== true,
    JSON.stringify(toolResult),
  );
}

async function testIdleInterruptFallbackNoIsIdle() {
  // No isIdle() on ctx -> requestInterrupt falls back to !activeResponseId.
  // Between turns activeResponseId is null, so this must behave as idle.
  const h = makeHarness();
  const idleCtx = makeCtx({}); // no isIdle method
  await h.handlers.session_start({}, idleCtx);

  await deliverInterrupt(h);

  assert(
    "idle interrupt (activeResponseId fallback) does not arm the window",
    idleCtx.abortCount === 0,
    `abortCount=${idleCtx.abortCount}`,
  );

  const turnCtx = makeCtx({}); // no isIdle method
  await h.handlers.agent_start({}, turnCtx);
  await h.handlers.turn_start({ turnIndex: 1 }, turnCtx);
  const toolResult = await h.handlers.tool_call(
    { toolCallId: "t1", toolName: "do_thing", input: {} },
    turnCtx,
  );

  assert(
    "fresh turn after fallback idle interrupt is NOT aborted",
    turnCtx.abortCount === 0,
    `abortCount=${turnCtx.abortCount}`,
  );
  assert(
    "fresh turn's tool_call is NOT blocked (fallback)",
    !toolResult || toolResult.block !== true,
    JSON.stringify(toolResult),
  );
}

async function testMidTurnInterruptStillAborts() {
  // Regression guard: a genuine mid-turn interrupt must still abort and replay.
  const h = makeHarness();
  const turnCtx = makeCtx({ idle: false });
  await h.handlers.session_start({}, turnCtx); // starts the inbox poller
  await h.handlers.agent_start({}, turnCtx);
  await h.handlers.turn_start({ turnIndex: 1 }, turnCtx);

  await deliverInterrupt(h);

  assert(
    "mid-turn interrupt aborts the live turn",
    turnCtx.abortCount >= 1,
    `abortCount=${turnCtx.abortCount}`,
  );

  // Replay must keep aborting within the window and block in-flight tool calls.
  const toolResult = await h.handlers.tool_call(
    { toolCallId: "t1", toolName: "do_thing", input: {} },
    turnCtx,
  );
  assert(
    "mid-turn interrupt blocks subsequent tool_call (replay)",
    !!toolResult && toolResult.block === true,
    JSON.stringify(toolResult),
  );
}

async function testAgentLoopInterruptFallbackNoIsIdleBeforeTurnStart() {
  // No isIdle(), and an interrupt lands after agent_start but before
  // turn_start. Older SDKs without isIdle() still need to treat this as part of
  // the live agent loop, not as an idle interrupt to drop.
  const h = makeHarness();
  const turnCtx = makeCtx({}); // no isIdle method
  await h.handlers.session_start({}, turnCtx); // starts the inbox poller
  await h.handlers.agent_start({}, turnCtx);

  await deliverInterrupt(h);

  assert(
    "agent-loop interrupt aborts before turn_start (active loop fallback)",
    turnCtx.abortCount >= 1,
    `abortCount=${turnCtx.abortCount}`,
  );

  await h.handlers.turn_start({ turnIndex: 1 }, turnCtx);
  const toolResult = await h.handlers.tool_call(
    { toolCallId: "t1", toolName: "do_thing", input: {} },
    turnCtx,
  );
  assert(
    "agent-loop interrupt before turn_start replays to block tool_call",
    !!toolResult && toolResult.block === true,
    JSON.stringify(toolResult),
  );
}

async function testMidTurnInterruptFallbackNoIsIdle() {
  // No isIdle() but an agent loop is active -> must still arm.
  const h = makeHarness();
  const turnCtx = makeCtx({}); // no isIdle method
  await h.handlers.session_start({}, turnCtx); // starts the inbox poller
  await h.handlers.agent_start({}, turnCtx);
  await h.handlers.turn_start({ turnIndex: 1 }, turnCtx);

  await deliverInterrupt(h);

  assert(
    "mid-turn interrupt aborts (activeResponseId fallback)",
    turnCtx.abortCount >= 1,
    `abortCount=${turnCtx.abortCount}`,
  );
}

async function testAgentStartClearsStaleWindow() {
  // Belt-and-suspenders: even if a window is armed during a live turn, a brand
  // new agent loop must start clean and not abort its first tool call.
  const h = makeHarness();
  const turnCtx = makeCtx({ idle: false });
  await h.handlers.session_start({}, turnCtx); // starts the inbox poller
  await h.handlers.agent_start({}, turnCtx);
  await h.handlers.turn_start({ turnIndex: 1 }, turnCtx);
  await deliverInterrupt(h);
  assert(
    "window armed during live turn (precondition)",
    turnCtx.abortCount >= 1,
    `abortCount=${turnCtx.abortCount}`,
  );

  // A new agent loop begins (e.g. the user's next message) within 30s.
  const nextCtx = makeCtx({ idle: false });
  await h.handlers.agent_start({}, nextCtx);
  await h.handlers.turn_start({ turnIndex: 1 }, nextCtx);
  const toolResult = await h.handlers.tool_call(
    { toolCallId: "t2", toolName: "do_thing", input: {} },
    nextCtx,
  );

  assert(
    "new agent loop clears stale window (no abort)",
    nextCtx.abortCount === 0,
    `abortCount=${nextCtx.abortCount}`,
  );
  assert(
    "new agent loop's tool_call is NOT blocked",
    !toolResult || toolResult.block !== true,
    JSON.stringify(toolResult),
  );
}

async function testTaskPlanPublishesTodos() {
  const h = makeHarness({ captureEvents: true });
  await h.handlers.session_start({}, {});
  const tool = h.registeredTools.manage_todo_list;
  assert("registers manage_todo_list", !!tool);
  assert(
    "requires a task plan before multi-step work",
    tool.promptGuidelines.some((guideline) =>
      guideline.includes(
        "must call manage_todo_list before using other tools",
      ),
    ),
  );

  const result = await tool.execute("todo-1", {
    operation: "write",
    todoList: [
      {
        id: 1,
        title: "Trace rendering",
        description: "Tracing the shared event path",
        status: "in-progress",
      },
      {
        id: 2,
        title: "Run checks",
        description: "Run focused checks",
        status: "not-started",
      },
    ],
  });
  const event = h.postedEvents.find(
    (item) => item.type === "external_session_todos",
  );
  assert(
    "manage_todo_list publishes the shared todo event",
    JSON.stringify(event && event.data.todos) ===
      JSON.stringify([
        {
          content: "Trace rendering",
          status: "in_progress",
          activeForm: "Tracing the shared event path",
        },
        {
          content: "Run checks",
          status: "pending",
          activeForm: "Run focused checks",
        },
      ]),
    JSON.stringify(event),
  );
  assert(
    "manage_todo_list persists its current list in tool details",
    result.details.todos.length === 2 &&
      result.details.todos[0].status === "in-progress",
    JSON.stringify(result),
  );

  const restored = makeHarness({ captureEvents: true });
  await restored.handlers.session_start(
    {},
    {
      sessionManager: {
        getBranch: () => [
          {
            type: "message",
            message: {
              role: "toolResult",
              toolName: "manage_todo_list",
              details: { todos: result.details.todos },
            },
          },
        ],
      },
    },
  );
  const restoredEvent = restored.postedEvents.find(
    (item) => item.type === "external_session_todos",
  );
  assert(
    "session_start restores and republishes the latest task plan",
    restoredEvent && restoredEvent.data.todos.length === 2,
    JSON.stringify(restoredEvent),
  );
}

async function testExistingTaskToolIsMirroredWithoutConflict() {
  const h = makeHarness({
    captureEvents: true,
    existingTools: [{ name: "manage_todo_list" }],
  });
  await h.handlers.session_start({}, {});
  assert(
    "reuses an existing manage_todo_list tool",
    !h.registeredTools.manage_todo_list,
  );

  await h.handlers.tool_result(
    {
      toolCallId: "external-todo-1",
      toolName: "manage_todo_list",
      details: {
        todos: [
          {
            id: 1,
            title: "Use the shared tool",
            description: "Using the shared task tool",
            status: "in-progress",
          },
        ],
      },
    },
    {},
  );
  const event = h.postedEvents.find(
    (item) => item.type === "external_session_todos",
  );
  assert(
    "mirrors an existing task tool into the shared todo event",
    event && event.data.todos[0].content === "Use the shared tool",
    JSON.stringify(event),
  );
}

// Native instructions now ride the system prompt. User input reaches the
// mirror unchanged so pending-input reconciliation sees the original text.
async function testInputMirrorPreservesUserText() {
  const h = makeHarness({ captureEvents: true });
  await h.handlers.session_start({}, {});
  const userText = "<omnigent_agent_instructions>literal user text</omnigent_agent_instructions>";

  await h.handlers.input({ text: userText }, {});
  assert(
    "input preflight does not mirror a user message",
    !h.postedEvents.some((item) => item.type === "external_conversation_item"),
  );
  await h.handlers.message_end(
    { message: { role: "user", content: [{ type: "text", text: userText }] } },
    {},
  );

  const event = h.postedEvents.find(
    (item) => item.type === "external_conversation_item",
  );
  assert(
    "mirrored input preserves the user text",
    !!event && event.data.item_data.content[0].text === userText,
    JSON.stringify(event),
  );
}

async function testPeerReceiptRequiresConsumedUserMessage() {
  const h = makeHarness({ captureEvents: true });
  const envelope =
    `[Peer message from session ${"a".repeat(32)} msg=${"b".repeat(32)} ` +
    '"Sender" (Pi) ref=thread]\n\npeer input';
  const userItems = () => h.postedEvents.filter(
    (item) => item.type === "external_conversation_item" &&
      item.data.item_type === "message" && item.data.item_data.role === "user",
  );

  // A missing model rejects after input; no Pi user message is consumed.
  await h.handlers.input({ text: envelope }, {});
  assert("invalid-model preflight has no receipt", userItems().length === 0);

  // A mid-turn steer may remain queued while the current assistant finishes.
  await h.handlers.input({ text: envelope }, {});
  await h.handlers.message_end(
    { message: { role: "assistant", content: [{ type: "text", text: "working" }] } },
    {},
  );
  assert("pending steer has no receipt", userItems().length === 0);

  await h.handlers.message_end(
    { message: { role: "user", content: [{ type: "text", text: envelope }] } },
    {},
  );
  const consumed = userItems();
  assert("consumed steer mirrors once", consumed.length === 1, JSON.stringify(consumed));
  assert(
    "consumed envelope is exact",
    consumed[0].data.item_data.content[0].text === envelope,
  );
  assert(
    "receipt names the consumed-user source only",
    /^pi-consumed-user-/.test(consumed[0].data.response_id),
  );
}

// The web store only clears its local "streaming" flag when a turn's `idle`
// status edge carries the same response_id as the `running` edge that opened
// it. A fresh id per edge left the composer stuck queueing until a tab switch
// reset the store. Assert agent_start/agent_end share one id.
async function testRunningIdleShareResponseId() {
  const h = makeHarness({ captureEvents: true });
  const ctx = makeCtx({ idle: false });

  await h.handlers.agent_start({}, ctx);
  await h.handlers.message_end({
    message: { role: "assistant", stopReason: "stop", content: [] },
  }, ctx);
  await h.handlers.agent_end({ messages: [] }, ctx);

  const edges = statusEdges(h.postedEvents);
  const running = edges.find((e) => e.status === "running");
  const idle = edges.find((e) => e.status === "idle");

  assert(
    "agent_start posts a running edge with a response_id",
    running !== undefined && typeof running.responseId === "string" && running.responseId.length > 0,
    JSON.stringify(running),
  );
  assert(
    "agent_end posts an idle edge with a response_id",
    idle !== undefined && typeof idle.responseId === "string" && idle.responseId.length > 0,
    JSON.stringify(idle),
  );
  assert(
    "running and idle edges share the same response_id",
    running && idle && running.responseId === idle.responseId,
    `running=${running && running.responseId} idle=${idle && idle.responseId}`,
  );

  // A second turn mints a fresh id, still paired across its own running/idle.
  await h.handlers.agent_start({}, ctx);
  await h.handlers.message_end({
    message: { role: "assistant", stopReason: "stop", content: [] },
  }, ctx);
  await h.handlers.agent_end({ messages: [] }, ctx);
  const edges2 = statusEdges(h.postedEvents);
  const running2 = edges2.filter((e) => e.status === "running");
  const idle2 = edges2.filter((e) => e.status === "idle");
  assert(
    "second turn pairs its own running/idle id and differs from the first",
    running2.length === 2 &&
      idle2.length === 2 &&
      running2[1].responseId === idle2[1].responseId &&
      running2[1].responseId !== running2[0].responseId,
    `turn1=${running2[0].responseId} turn2=${running2[1].responseId}`,
  );
}

// Regression coverage for the tool-call transient-retry budget: a 5xx outage
// that lasts past the old 30s window must still be ridden out, not failed
// closed early. Time is faked (Date.now + the backoff sleep's setTimeout) so
// the test never waits; the window mirrors TOOL_CALL_POLICY_RETRY_BUDGET_S in
// omnigent/native/native_policy_hook.py.
async function testToolCallRetryOutlastsTheShortBudget() {
  const h = makeHarness({ captureEvents: true });
  await h.handlers.session_start({}, {});

  const realFetch = global.fetch;
  const realNow = Date.now;
  const realSetTimeout = global.setTimeout;
  const clock = { t: 0 };
  Date.now = () => clock.t;
  global.setTimeout = (fn, ms) => {
    if (ms <= 30_000) {
      // A retry-backoff sleep: advance the fake clock, then resolve.
      clock.t += ms;
      queueMicrotask(fn);
      return 0;
    }
    // Long internal timers (the 240s park abort) must not fire here.
    return realSetTimeout(() => {}, ms);
  };

  const attemptTimes = [];
  global.fetch = async () => {
    attemptTimes.push(clock.t);
    return { ok: false, status: 503 };
  };

  let verdict;
  try {
    verdict = await h.handlers.tool_call(
      { toolCallId: "t1", toolName: "read_file", input: {} },
      makeCtx({ idle: false }),
    );
  } finally {
    global.fetch = realFetch;
    Date.now = realNow;
    global.setTimeout = realSetTimeout;
  }

  assert(
    "a 5xx outage still fails the tool call closed",
    !!verdict && verdict.block === true,
    JSON.stringify(verdict),
  );
  const lastAttemptMs = attemptTimes[attemptTimes.length - 1];
  assert(
    "the retry loop outlives the old 30s transient budget",
    lastAttemptMs > 30_000,
    `lastAttemptMs=${lastAttemptMs} attempts=${attemptTimes.length}`,
  );
}
async function uiSession(mode = "tui", missing = []) {
  const h = makeHarness({ captureEvents: true });
  const originalCalls = [];
  const ui = {};
  for (const [method, result] of Object.entries({
    confirm: true, select: "Allow", input: "typed", editor: "edited",
    notify: undefined, setStatus: undefined, setTitle: undefined,
  })) {
    ui[method] = function (...args) {
      assert(`${method}: fallback retains UI receiver`, this === ui);
      originalCalls.push({ method, args });
      return result;
    };
  }
  for (const method of missing) delete ui[method];
  const ctx = { mode, ui };
  await h.handlers.session_start({}, ctx);
  originalCalls.length = 0;
  return { ...h, ctx, originalCalls };
}

function dialogFetch(respond) {
  global.fetch = async (url, opts) => String(url).includes("pi-extension-ui")
    ? respond(opts) : { ok: true, status: 204 };
}

function dialogResponse(value) {
  return { ok: true, status: 200, text: async () => JSON.stringify(value) };
}

async function testTuiWrapParksConfirmWithoutCallingOriginal() {
  const { ctx, originalCalls } = await uiSession();
  dialogFetch((opts) => {
    const body = JSON.parse(opts.body);
    assert("confirm carries its correlation ID and request", body.elicitation_id &&
      body.request.method === "confirm" && body.request.title === "Clear?");
    return dialogResponse({ confirmed: true });
  });
  assert("confirm returns the server answer", await ctx.ui.confirm("Clear?", "Lost.") === true);
  assert("original TUI dialog is bypassed", !originalCalls.some((call) => call.method === "confirm"));
  ctx.ui.notify("hello");
  ctx.ui.setStatus("test", "ready");
  assert("notifications remain local", originalCalls.some((call) => call.method === "notify") && originalCalls.some((call) => call.method === "setStatus"));
}

async function testUnsupportedDialogsFallBackToTui() {
  for (const status of [404, 405, 403]) {
    const { ctx, originalCalls } = await uiSession();
    dialogFetch(() => ({ ok: false, status }));
    const opts = { timeout: 2000 };
    for (const [method, args, expected] of [
      ["confirm", ["Continue?", "Details", opts], true],
      ["select", ["Choice?", ["Allow", "Block"], opts], "Allow"],
      ["input", ["Name?", "placeholder", opts], "typed"],
      ["editor", ["Notes", "prefill", opts], "edited"],
    ]) {
      const actual = await ctx.ui[method](...args);
      assert(`${status}: ${method} result`, actual === (status === 403 ? (method === "confirm" ? false : undefined) : expected));
      if (status !== 403) {
        const call = originalCalls.at(-1);
        assert(`${method}: preserves arguments`, call.method === method && call.args.every((arg, index) => arg === args[index]));
      }
    }
    assert(`${status}: only unsupported endpoints use TUI`, originalCalls.length === (status === 403 ? 0 : 4));
  }
}

async function testDialogCancellationDoesNotRepark() {
  for (const [response, expectedCalls] of [
    [dialogResponse({ cancelled: true }), 1],
    [{ ok: true, status: 200, text: async () => "" }, 3],
  ]) {
    const { ctx } = await uiSession();
    let calls = 0;
    dialogFetch(() => { calls += 1; return response; });
    assert("unanswered input returns undefined", await ctx.ui.input("Name?", "", { timeout: 6000 }) === undefined);
    assert("cancel stops and empty responses are bounded", calls === expectedCalls);
  }
}

async function testTuiWrapReparksSameElicitationId() {
  const { ctx, configPath } = await uiSession();
  const ids = [], headers = [];
  const config = JSON.parse(fs.readFileSync(configPath, "utf8"));
  const setToken = (token) => fs.writeFileSync(configPath, JSON.stringify({ ...config, authHeaders: { authorization: token } }));
  setToken("first-token");
  dialogFetch((opts) => {
    const body = JSON.parse(opts.body);
    ids.push(body.elicitation_id);
    headers.push(opts.headers.authorization);
    assert("caller timeout reaches the server", body.request.timeout === 6000);
    if (ids.length === 1) {
      setToken("rotated-token");
      return { ok: true, status: 200, text: async () => "" };
    }
    return dialogResponse({ value: "Allow" });
  });
  const started = Date.now();
  assert("select returns chosen label", await ctx.ui.select("Choice?", ["Allow", "Block"], { timeout: 6000 }) === "Allow");
  assert("empty responses back off", Date.now() - started >= 900);
  assert("re-park preserves its ID", ids.length === 2 && ids[0] === ids[1]);
  assert("re-park refreshes the bearer", JSON.stringify(headers) === JSON.stringify(["first-token", "rotated-token"]));
}

async function testPartialUiStillInitializes() {
  const { ctx, registeredTools } = await uiSession("tui", ["editor"]);
  assert("missing editor stays absent", ctx.ui.editor === undefined);
  assert("startup registers task tool", !!registeredTools.manage_todo_list);
  dialogFetch(() => dialogResponse({ confirmed: true }));
  assert("remaining UI still works", await ctx.ui.confirm("Continue?", "") === true);
}

async function testActiveDialogCancellation() {
  for (const phase of ["already-aborted", "headers", "body", "backoff", "timeout"]) {
    const { ctx } = await uiSession();
    const controller = new AbortController();
    let requestStarted;
    const started = new Promise((resolve) => { requestStarted = resolve; });
    dialogFetch((opts) => {
      const wait = () => new Promise((_, reject) => {
        opts.signal.addEventListener("abort", () => reject(new Error("aborted")), { once: true });
        requestStarted();
      });
      if (phase === "headers" || phase === "timeout") return wait();
      if (phase === "body") return { ok: true, status: 200, text: wait };
      setTimeout(requestStarted, 10);
      return { ok: false, status: 503 };
    });
    if (phase === "already-aborted") controller.abort();
    const pending = ctx.ui.confirm("Continue?", "Test only", {
      ...(phase !== "timeout" ? { signal: controller.signal } : {}),
      timeout: phase === "timeout" ? 50 : 2000,
    });
    if (phase !== "already-aborted" && phase !== "timeout") {
      await started;
      controller.abort();
    }
    let timer;
    try {
      const value = await Promise.race([
        pending,
        new Promise((resolve) => { timer = setTimeout(() => resolve("still-waiting"), 500); }),
      ]);
      assert(`${phase}: abort releases the caller`, value === false, String(value));
    } finally {
      clearTimeout(timer);
    }
    dialogFetch(() => dialogResponse({ confirmed: true }));
    assert(`${phase}: a later dialog works`, await ctx.ui.confirm("Again?", "Test only") === true);
  }
}

async function testRpcModeDoesNotWrapUi() {
  const { ctx, originalCalls } = await uiSession("rpc");
  assert("RPC mode keeps its original UI", await ctx.ui.confirm("Clear?", "Lost.") === true && originalCalls.some((call) => call.method === "confirm"));
}

async function testSessionStartupDoesNotCompleteATurn() {
  const h = makeHarness({ captureEvents: true });
  const ctx = makeCtx({ idle: true });
  ctx.sessionManager = { getSessionId: () => "saved-native-session" };

  await h.handlers.session_start({}, ctx);

  assert(
    "starting an idle or resumed session does not report a completed turn",
    statusEdges(h.postedEvents).length === 0,
    JSON.stringify(statusEdges(h.postedEvents)),
  );
}

async function testSessionStartMarksInputReady() {
  const h = makeHarness();
  const marker = path.join(h.bridgeDir, "input_ready");
  assert("input-ready marker is absent before session_start", !fs.existsSync(marker));

  await h.handlers.session_start({}, makeCtx({ idle: true }));

  assert(
    "session_start writes the input-ready marker once the inbox poller is armed",
    fs.existsSync(marker) && !!h.pi.__omnigentInboxPoller,
  );
}

async function testQueuedPromptDuringStartupStaysRunningUntilAgentEnd() {
  const h = makeHarness({ captureEvents: true });
  const ctx = makeCtx({ idle: true });
  ctx.sessionManager = { getSessionId: () => "queued-native-session" };
  const captureFetch = global.fetch;
  let releaseStartupPatch;
  const startupPatchPending = new Promise((resolve) => {
    releaseStartupPatch = resolve;
  });
  global.fetch = async (url, opts) => {
    if (opts.method === "PATCH") await startupPatchPending;
    return captureFetch(url, opts);
  };
  let agentStartPromise;
  h.pi.sendUserMessage = () => {
    ctx.isIdle = () => false;
    agentStartPromise = h.handlers.agent_start({}, ctx);
  };
  const payloadPath = path.join(h.inboxDir, "first-prompt.json");
  fs.writeFileSync(
    payloadPath,
    JSON.stringify({ id: "first-prompt", type: "user_message", content: "Run the task" }),
  );
  const startup = h.handlers.session_start({}, ctx);
  try {
    const deadline = Date.now() + 3000;
    while (fs.existsSync(payloadPath)) {
      if (Date.now() > deadline) throw new Error("startup prompt was not consumed by poller");
      await sleep(20);
    }
    if (!agentStartPromise) throw new Error("the queued prompt did not start an agent loop");
    await agentStartPromise;
    releaseStartupPatch();
    await startup;

    const beforeEnd = statusEdges(h.postedEvents);
    assert(
      "finishing startup preserves the queued prompt's running turn",
      beforeEnd.length === 1 && beforeEnd[0].status === "running",
      JSON.stringify(beforeEnd),
    );

    await h.handlers.message_end({
      message: { role: "assistant", stopReason: "stop", content: [] },
    }, ctx);
    await h.handlers.agent_end({ messages: [] }, ctx);
    const afterEnd = statusEdges(h.postedEvents);
    assert(
      "only agent_end completes the queued prompt with the same response_id",
      afterEnd.length === 2 &&
        afterEnd[0].status === "running" &&
        afterEnd[1].status === "idle" &&
        afterEnd[0].responseId === afterEnd[1].responseId,
      JSON.stringify(afterEnd),
    );
  } finally {
    releaseStartupPatch();
    await startup;
    global.fetch = captureFetch;
  }
}

async function testPiRotation(fault = "none") {
  const h = makeHarness({ captureEvents: true });
  const calls = [];
  const previousFetch = global.fetch;
  let failed = false;
  let prepared = fault === "startup-recovery";
  let creations = prepared ? 1 : 0;
  global.fetch = async (url, options) => {
    const method = options.method;
    const pathname = new URL(url).pathname;
    const body = options.body && JSON.parse(options.body);
    calls.push({ method, pathname, body });
    let data = {};
    if (method === "GET") data = {
      agent_id: "ag_pi", runner_id: "runner_pi",
      archive_revision: 0,
      labels: fault === "startup-recovery" ? {
        "omnigent.rotation_successor": JSON.stringify({ id: "conv_next", revision: 0 }),
      } : {},
    };
    if (pathname.endsWith("/successor")) {
      if (!prepared) { prepared = true; creations += 1; }
      if (fault === "lost-response" && !failed) {
        failed = true;
        throw new Error("response lost after server commit");
      }
      data = { id: "conv_next" };
    }
    if (method === "PATCH" && fault === "bind" && !failed) {
      failed = true;
      return { status: 503, json: async () => ({}) };
    }
    if (pathname.endsWith("/succession")) {
      if (fault === "retired") {
        data = { status: "retired", successor_id: "actual_successor" };
      } else if (fault === "pending" && !failed) {
        failed = true;
        data = { status: "pending" };
      } else if (fault === "refused" && !failed) {
        failed = true;
        return { status: 409, json: async () => ({}) };
      } else data = { status: "done" };
    }
    return { status: 200, json: async () => data };
  };
  const ctx = makeCtx({ idle: true });
  ctx.sessionManager = { getSessionId: () => "pi_new_context" };
  try {
    await h.handlers.session_start({ reason: fault === "startup-recovery" ? "startup" : "new" }, ctx);
    if (fault === "retired") {
      assert("retired: never retries", !h.pi.__omnigentRotationRetry);
      assert("retired: source pane retained", !calls.some(c => c.method === "DELETE"));
      const blocked = await h.handlers.tool_call({ toolCallId: "late", toolName: "write", input: {} }, ctx);
      assert("retired: old tools stay fenced", blocked.block === true);
      const count = calls.length;
      await h.handlers.session_start({ reason: "startup" }, ctx);
      assert("retired: restart never retries", calls.length === count && !h.pi.__omnigentRotationRetry);
      const config = JSON.parse(fs.readFileSync(path.join(h.bridgeDir, "config.json"), "utf8"));
      assert("retired: successor retained for recovery", config.retiredSuccessorId === "actual_successor");
      return;
    }
    if (["lost-response", "bind", "pending", "refused"].includes(fault)) {
      assert(`${fault}: keeps old pane until completed`, !calls.some(c => c.method === "DELETE"));
      assert(`${fault}: no old identity rebound`, !calls.some(c => c.body && c.body.external_session_id));
      const blocked = await h.handlers.tool_call({ toolCallId: "late", toolName: "write", input: {} }, ctx);
      assert(`${fault}: pending source tools are fenced`, blocked.block === true);
      const deadline = Date.now() + 3000;
      while (!calls.some(c => c.method === "DELETE") && Date.now() < deadline) await sleep(20);
    }
    assert(`${fault}: one physical successor`, creations === 1);
    assert(`${fault}: uses source-scoped preparation`, calls.some(c => c.pathname === "/v1/sessions/conv_test/successor"));
    assert(`${fault}: no raw-label generic create`, !calls.some(c => c.pathname === "/v1/sessions"));
    assert(`${fault}: successor bound before succession`, calls.findIndex(c => c.method === "PATCH" && c.body.runner_id) < calls.findIndex(c => c.pathname.endsWith("/succession")));
    assert(`${fault}: fresh terminal ensured`, calls.some(c => c.body && c.body.ensure_native_terminal === true));
    assert(`${fault}: fresh pane owns its external identity`, !calls.some(c => c.body && c.body.external_session_id === "pi_new_context"));
    assert(`${fault}: children and empty mother carried`, calls.some(c => c.pathname.endsWith("/succession") && c.body.allow_empty === true));
    assert(`${fault}: old pane eventually retires`, calls.some(c => c.method === "DELETE" && c.pathname.endsWith("/resources/terminals/terminal_pi_main")));
    const config = JSON.parse(fs.readFileSync(path.join(h.bridgeDir, "config.json"), "utf8"));
    assert(`${fault}: pending marker cleared`, !config.pendingRotationSource && !config.pendingSuccessorId);
  } finally {
    clearTimeout(h.pi.__omnigentRotationRetry);
    global.fetch = previousFetch;
  }
}

async function testChildResetDoesNotCreateMother() {
  const h = makeHarness({ captureEvents: true });
  const calls = [];
  const previousFetch = global.fetch;
  global.fetch = async (url, options) => {
    calls.push({ method: options.method, pathname: new URL(url).pathname });
    return { status: 200, json: async () => ({ parent_session_id: "mother" }) };
  };
  try {
    await h.handlers.session_start({ reason: "new" }, makeCtx({ idle: true }));
    assert("child reset never creates a mother", !calls.some(c => c.pathname.endsWith("/successor")));
    assert("child reset never reparents or retires itself", !calls.some(c => c.pathname.endsWith("/succession") || c.method === "DELETE"));
    const config = JSON.parse(fs.readFileSync(path.join(h.bridgeDir, "config.json"), "utf8"));
    assert("child remains usable after its own reset", !config.pendingRotationSource && !!h.pi.__omnigentInboxPoller);
  } finally { global.fetch = previousFetch; }
}

(async () => {
  try {
    await testStartupInstructionsReachEveryAgentStart();
    await testSessionStartupDoesNotCompleteATurn();
    await testSessionStartMarksInputReady();
    await testQueuedPromptDuringStartupStaysRunningUntilAgentEnd();
    for (const fault of ["none", "lost-response", "bind", "pending", "refused", "startup-recovery", "retired"])
      await testPiRotation(fault);
    await testChildResetDoesNotCreateMother();
    await testRunningIdleShareResponseId();
    await testTaskPlanPublishesTodos();
    await testExistingTaskToolIsMirroredWithoutConflict();
    await testInputMirrorPreservesUserText();
    await testPeerReceiptRequiresConsumedUserMessage();
    await testToolCallRetryOutlastsTheShortBudget();
    await testIdleInterruptDoesNotPoisonNextTurn();
    await testIdleInterruptFallbackNoIsIdle();
    await testMidTurnInterruptStillAborts();
    await testAgentLoopInterruptFallbackNoIsIdleBeforeTurnStart();
    await testMidTurnInterruptFallbackNoIsIdle();
    await testAgentStartClearsStaleWindow();
    await testTuiWrapParksConfirmWithoutCallingOriginal();
    await testUnsupportedDialogsFallBackToTui();
    await testDialogCancellationDoesNotRepark();
    await testTuiWrapReparksSameElicitationId();
    await testPartialUiStillInitializes();
    await testActiveDialogCancellation();
    await testRpcModeDoesNotWrapUi();
  } finally {
    for (const h of harnesses) {
      if (h.pi.__omnigentInboxPoller) clearInterval(h.pi.__omnigentInboxPoller);
      clearTimeout(h.pi.__omnigentRotationRetry);
      try {
        fs.rmSync(h.bridgeDir, { recursive: true, force: true });
      } catch (_err) {}
    }
  }
})();
