// Tests for the per-conversation browser-view registry (src/browserViewRegistry.js),
// run with `node --test` (no extra deps). The registry is a pure factory — all
// Electron deps are injected — so we drive it with stub views + spies.
//
// The load-bearing case is the FIRST-navigate activation signal: on a fresh
// conversation, openOrNavigate creates the view DETACHED (activeConversationId
// is null), so no `browser-host-active-changed` fires. Without the
// `browser-view-created` emit the React pane would never learn a view exists,
// never mount its placeholder, and never call setActive — the pane would stay
// invisible (the bug this test guards against).

const { describe, it, beforeEach } = require("node:test");
const assert = require("node:assert/strict");

const { createBrowserViewRegistry, agentPartition } = require("../src/browserViewRegistry");
const { createBrowserViewBoundsController } = require("../src/browserViewBounds");

// Private IPv4 fixtures are built from octets so no LAN address literal enters the source.
const privateIpv4Address = (...octets) => octets.join(".");

/** Build a registry with spy-backed injected deps. Returns the registry plus
 *  the recorded renderer sends / attach / detach calls for assertions. */
function makeRegistry() {
  const sent = []; // { channel, payload }
  const attached = [];
  const detached = [];
  const visibility = []; // booleans passed to any view's setVisible, in order
  const makeStubView = () => ({
    setBounds() {},
    setVisible(v) {
      visibility.push(v);
    },
    webContents: {
      loadURL() {},
      close() {},
      removeListener() {},
      on() {},
      setWindowOpenHandler() {},
    },
  });
  const registry = createBrowserViewRegistry({
    WebContentsViewCtor: () => makeStubView(),
    createBoundsController: createBrowserViewBoundsController,
    attachToHost: (view) => attached.push(view),
    detachFromHost: (view) => detached.push(view),
    sendToRenderer: (channel, payload) => sent.push({ channel, payload }),
    getHostZoomFactor: () => 1,
  });
  return { registry, sent, attached, detached, visibility };
}

describe("browserViewRegistry — first-navigate activation signal", () => {
  let ctx;
  beforeEach(() => {
    ctx = makeRegistry();
  });

  it("keeps the agent view and two user tabs independent through switch and close", () => {
    const ids = ["conv_1", "browser-tab:conv_1:first", "browser-tab:conv_1:second"];
    for (const id of ids) ctx.registry.openOrNavigate(id, "https://example.com");
    const views = ids.map((id) => ctx.registry.get(id).view);
    assert.equal(new Set(views).size, 3);
    ctx.registry.setActive(ids[1]);
    ctx.registry.setActive(ids[2]);
    assert.deepEqual(ctx.detached, [views[1]]);
    ctx.registry.close(ids[1]);
    assert.equal(ctx.registry.get(ids[1]), null);
    assert.equal(ctx.registry.get(ids[0]).view, views[0]);
    assert.equal(ctx.registry.get(ids[2]).view, views[2]);
    assert.equal(ctx.registry.activeConversationId(), ids[2]);
  });

  it("emits browser-view-created on first openOrNavigate for a fresh conversation", () => {
    const r = ctx.registry.openOrNavigate("conv_1", "https://example.com");
    assert.equal(r.ok, true);
    assert.equal(r.created, true);
    const created = ctx.sent.filter((s) => s.channel === "browser-view-created");
    assert.equal(created.length, 1, "exactly one create event");
    assert.deepEqual(created[0].payload, { conversationId: "conv_1" });
  });

  it("creates the view DETACHED when the conversation isn't active (no attach, no host-active event)", () => {
    ctx.registry.openOrNavigate("conv_1", "https://example.com");
    // Fresh conversation → activeConversationId stayed null → never attached.
    assert.equal(ctx.attached.length, 0, "view is created detached");
    assert.equal(ctx.registry.activeConversationId(), null);
    const active = ctx.sent.filter((s) => s.channel === "browser-host-active-changed");
    assert.equal(active.length, 0, "no host-active event on detached create");
  });

  it("does NOT re-emit browser-view-created on a subsequent navigate of the same conversation", () => {
    ctx.registry.openOrNavigate("conv_1", "https://example.com");
    ctx.registry.openOrNavigate("conv_1", "https://example.org");
    const created = ctx.sent.filter((s) => s.channel === "browser-view-created");
    assert.equal(created.length, 1, "create fires once, on first create only");
  });

  it("setActive after create attaches the view and fires host-active-changed", () => {
    ctx.registry.openOrNavigate("conv_1", "https://example.com");
    const r = ctx.registry.setActive("conv_1");
    assert.equal(r.ok, true);
    assert.equal(ctx.attached.length, 1, "setActive attaches the view");
    assert.equal(ctx.registry.activeConversationId(), "conv_1");
    const active = ctx.sent.filter(
      (s) => s.channel === "browser-host-active-changed" && s.payload.conversationId === "conv_1",
    );
    assert.equal(active.length, 1);
  });

  it("has() reports view existence for the re-mount probe", () => {
    assert.equal(ctx.registry.has("conv_1"), false);
    ctx.registry.openOrNavigate("conv_1", "https://example.com");
    assert.equal(ctx.registry.has("conv_1"), true);
  });

  it("full first-navigate path: create signal → setActive → attached + bounds synced", () => {
    // Mirrors what BrowserPane does: it learns of the view from the create
    // event, mounts the placeholder, then setActive attaches + syncs bounds.
    // (the pane's onBrowserViewCreated listener)
    ctx.sent.length = 0;
    ctx.registry.openOrNavigate("conv_1", "https://example.com");
    const sawCreate = ctx.sent.find((s) => s.channel === "browser-view-created");
    assert.ok(sawCreate, "pane would receive the create event");

    // Pane reacts: setActive(conversationId) then a resize (bounds).
    ctx.registry.setActive("conv_1");
    const entry = ctx.registry.get("conv_1");
    assert.ok(entry, "entry resolvable for resize");
    entry.boundsController.setRendererBounds({
      x: 10,
      y: 20,
      width: 300,
      height: 400,
      devicePixelRatio: 1,
    });
    assert.equal(ctx.attached.length, 1, "view attached to host");
  });

  it("close() detaches any design-mode console listener stored on the entry", () => {
    ctx.registry.openOrNavigate("conv_1", "https://example.com");
    const entry = ctx.registry.get("conv_1");
    // Simulate what browserIpc's enable-design-mode handler does: stash a
    // listener + its webContents on the entry. close() must detach it so a
    // destroyed view leaves no dangling console-message listener.
    let removed = null;
    const handler = () => {};
    entry.designModeListener = handler;
    entry.designModeWebContents = {
      removeListener: (evt, fn) => {
        removed = { evt, fn };
      },
    };
    const r = ctx.registry.close("conv_1");
    assert.equal(r.removed, true);
    assert.deepEqual(removed, { evt: "console-message", fn: handler });
    assert.equal(entry.designModeListener, null);
    assert.equal(entry.designModeWebContents, null);
  });
});

function makeRecentSessionInputRegistry({
  isHostFocused = () => true,
  supportsRecentSessionSwitch = true,
} = {}) {
  const listeners = new Map();
  const sent = [];
  const webContents = {
    loadURL() {},
    close() {},
    removeListener() {},
    on(name, listener) {
      listeners.set(name, listener);
    },
    setWindowOpenHandler() {},
  };
  const registry = createBrowserViewRegistry({
    WebContentsViewCtor: () => ({ setBounds() {}, webContents }),
    createBoundsController: createBrowserViewBoundsController,
    attachToHost() {},
    detachFromHost() {},
    sendToRenderer: (channel, payload) => sent.push({ channel, payload }),
    isHostFocused,
  });
  registry.openOrNavigate("conv_1", "https://example.com");
  registry.setActive("conv_1");
  registry.setRecentSessionSwitchSupported(supportsRecentSessionSwitch);
  return { registry, listeners, sent };
}

describe("browserViewRegistry — recent-session input forwarding", () => {
  it("leaves page shortcuts untouched until the renderer advertises support", () => {
    const { listeners, sent } = makeRecentSessionInputRegistry({
      supportsRecentSessionSwitch: false,
    });
    const forward = listeners.get("before-input-event");
    const prevented = [];
    const event = () => ({ preventDefault: () => prevented.push(true) });

    forward(event(), { type: "keyDown", key: "Tab", code: "Tab", control: true });
    forward(event(), { type: "keyDown", key: "Escape", code: "Escape", control: true });

    assert.equal(prevented.length, 0);
    assert.equal(sent.filter((item) => item.channel === "browser-recent-session-input").length, 0);
  });

  it("forwards Ctrl+Tab, Control release, and Escape from an embedded page", () => {
    const { listeners, sent } = makeRecentSessionInputRegistry();
    const forward = listeners.get("before-input-event");
    const prevented = [];
    const event = () => ({ preventDefault: () => prevented.push(true) });
    forward(event(), { type: "keyDown", key: "Tab", code: "Tab", control: false });
    forward(event(), {
      type: "keyDown",
      key: "Tab",
      code: "Tab",
      control: true,
      shift: true,
    });
    forward(event(), {
      type: "keyDown",
      key: "Escape",
      code: "Escape",
      control: true,
    });
    forward(event(), {
      type: "keyUp",
      key: "Control",
      code: "ControlLeft",
      control: false,
    });
    forward(event(), { type: "keyDown", key: "Tab", code: "Tab", control: true });
    forward(event(), {
      type: "keyUp",
      key: "Control",
      code: "ControlLeft",
      control: false,
    });

    const forwarded = sent.filter((item) => item.channel === "browser-recent-session-input");
    assert.deepEqual(
      forwarded.map((item) => [item.payload.type, item.payload.key, item.payload.shiftKey]),
      [
        ["keydown", "Tab", true],
        ["keydown", "Escape", false],
        ["keydown", "Tab", false],
        ["keyup", "Control", false],
      ],
    );
    assert.equal(prevented.length, 3, "claim Tab and Escape keydowns, not Control release");
  });

  it("clears a pending switch on focus loss without intercepting a later Escape", () => {
    let hostFocused = true;
    const { listeners, sent } = makeRecentSessionInputRegistry({
      isHostFocused: () => hostFocused,
    });
    const forward = listeners.get("before-input-event");
    const blur = listeners.get("blur");
    const prevented = [];
    const event = () => ({ preventDefault: () => prevented.push(true) });

    forward(event(), { type: "keyDown", key: "Tab", code: "Tab", control: true });
    hostFocused = false;
    blur();
    const countAfterBlur = sent.length;
    forward(event(), { type: "keyDown", key: "Escape", code: "Escape", control: false });

    assert.equal(prevented.length, 1, "only Ctrl+Tab is claimed");
    assert.equal(sent.length, countAfterBlur, "ordinary Escape is not forwarded");
    assert.equal(sent.at(-1).payload.key, "Escape", "focus loss cancels the renderer switcher");
  });

  it("leaves later page input alone when the renderer declines the switch", () => {
    const { registry, listeners, sent } = makeRecentSessionInputRegistry();
    const forward = listeners.get("before-input-event");
    const prevented = [];
    const event = () => ({ preventDefault: () => prevented.push(true) });

    forward(event(), { type: "keyDown", key: "Tab", code: "Tab", control: true });
    registry.cancelRecentSessionSwitch();
    const countAfterCancellation = sent.length;
    forward(event(), { type: "keyDown", key: "Escape", code: "Escape", control: false });

    assert.equal(prevented.length, 1, "only Ctrl+Tab is claimed");
    assert.equal(sent.length, countAfterCancellation, "ordinary Escape is not forwarded");
  });

  it("releases a pending switch when renderer support ends", () => {
    const { registry, listeners, sent } = makeRecentSessionInputRegistry();
    const forward = listeners.get("before-input-event");
    const prevented = [];
    const event = () => ({ preventDefault: () => prevented.push(true) });

    forward(event(), { type: "keyDown", key: "Tab", code: "Tab", control: true });
    registry.setRecentSessionSwitchSupported(false);
    const countAfterUnsubscribe = sent.length;
    forward(event(), { type: "keyDown", key: "Escape", code: "Escape", control: false });

    assert.equal(prevented.length, 1, "only Ctrl+Tab is claimed");
    assert.equal(sent.length, countAfterUnsubscribe, "ordinary Escape is not forwarded");
  });
});

// Build a registry whose stub views record every loadURL call, so we can
// assert the agent-navigation allowlist blocks BEFORE loadURL is reached.
function makeLoadTrackingRegistry() {
  const loaded = []; // urls passed to loadURL
  const registry = createBrowserViewRegistry({
    WebContentsViewCtor: () => ({
      setBounds() {},
      webContents: {
        loadURL(u) {
          loaded.push(u);
        },
        close() {},
        removeListener() {},
        on() {},
        setWindowOpenHandler() {},
      },
    }),
    createBoundsController: createBrowserViewBoundsController,
    attachToHost() {},
    detachFromHost() {},
    sendToRenderer() {},
    getHostZoomFactor: () => 1,
  });
  return { registry, loaded };
}

describe("browserViewRegistry — agent-navigation allowlist", () => {
  it("rejects agent navigation to file:// and never calls loadURL", () => {
    const { registry, loaded } = makeLoadTrackingRegistry();
    const r = registry.openOrNavigate("conv_1", "file:///home/user/.ssh/id_rsa", undefined, {
      agent: true,
    });
    assert.equal(r.ok, false);
    assert.match(r.error, /navigation blocked/);
    assert.equal(loaded.length, 0, "loadURL must not run on a rejected agent nav");
  });

  it("rejects agent navigation to the cloud-metadata IP (169.254.169.254)", () => {
    const { registry, loaded } = makeLoadTrackingRegistry();
    const r = registry.openOrNavigate(
      "conv_1",
      "http://169.254.169.254/latest/meta-data/",
      undefined,
      {
        agent: true,
      },
    );
    assert.equal(r.ok, false);
    assert.equal(loaded.length, 0);
  });

  it("rejects agent navigation to localhost", () => {
    const { registry, loaded } = makeLoadTrackingRegistry();
    const r = registry.openOrNavigate("conv_1", "http://localhost:6767/health", undefined, {
      agent: true,
    });
    assert.equal(r.ok, false);
    assert.equal(loaded.length, 0);
  });

  it("allows agent navigation to a normal public https URL (loadURL runs)", () => {
    const { registry, loaded } = makeLoadTrackingRegistry();
    const r = registry.openOrNavigate("conv_1", "https://example.com/", undefined, {
      agent: true,
    });
    assert.equal(r.ok, true);
    assert.deepEqual(loaded, ["https://example.com/"]);
  });

  it("does NOT gate a direct registry call without opts.agent — file:// still loads", () => {
    // Direct registry calls can omit opts.agent; renderer IPC always sets it.
    const { registry, loaded } = makeLoadTrackingRegistry();
    const r = registry.openOrNavigate("conv_1", "file:///tmp/report.html", undefined, {
      force: true,
    });
    assert.equal(r.ok, true);
    assert.deepEqual(loaded, ["file:///tmp/report.html"]);
  });
});

// A stub view that captures the webContents event handlers (will-navigate /
// will-redirect / will-frame-navigate) and the window-open handler, so tests
// can fire a redirect and assert whether it was cancelled (preventDefault).
function makeEventCapturingRegistry(extra = {}) {
  const sent = []; // { channel, payload }
  const loaded = []; // loadURL targets on the created view
  const clipboardWrites = []; // copyTextToClipboard payloads
  const externalOpens = []; // openUrlExternal payloads
  const menus = []; // showContextMenu item lists
  let copyCalls = 0; // webContents.copy() invocations
  let handlers; // { [event]: fn } for the single created view
  let windowOpenHandler;
  let nextWebContentsId = 1;
  const registry = createBrowserViewRegistry({
    WebContentsViewCtor: () => {
      handlers = {};
      return {
        setBounds() {},
        webContents: {
          id: nextWebContentsId++,
          loadURL(url) {
            loaded.push(url);
          },
          close() {},
          copy() {
            copyCalls += 1;
          },
          removeListener() {},
          on(event, fn) {
            handlers[event] = fn;
          },
          setWindowOpenHandler(fn) {
            windowOpenHandler = fn;
          },
        },
      };
    },
    createBoundsController: createBrowserViewBoundsController,
    attachToHost() {},
    detachFromHost() {},
    sendToRenderer: (channel, payload) => sent.push({ channel, payload }),
    getHostZoomFactor: () => 1,
    openUrlExternal: (url) => externalOpens.push(url),
    copyTextToClipboard: (text) => clipboardWrites.push(text),
    showContextMenu: (items) => menus.push(items),
    ...extra,
  });
  return {
    registry,
    sent,
    loaded,
    clipboardWrites,
    externalOpens,
    menus,
    copyCalls: () => copyCalls,
    fire: (event, targetUrl) => {
      const ev = {
        url: targetUrl,
        prevented: false,
        preventDefault() {
          this.prevented = true;
        },
      };
      handlers[event](ev, targetUrl);
      return ev;
    },
    fireContextMenu: (params) => handlers["context-menu"]({}, params),
    windowOpen: (url) => windowOpenHandler({ url }),
  };
}

describe("browserViewRegistry — redirect/nav guard (SSRF: allowlist on every hop)", () => {
  it("uses the same live Arca context for initial URL, redirect, frame and popup", () => {
    let eligible = true;
    const context = { serverTarget: "selected-server", sourceHostId: "actual-host" };
    const { registry, fire, windowOpen, loaded } = makeEventCapturingRegistry({
      allowsLocalhost: (ctx) => eligible && ctx === context,
    });
    assert.equal(
      registry.openOrNavigate("arca", "http://localhost:5173", undefined, {
        agent: true,
        agentContext: context,
      }).ok,
      true,
    );
    for (const event of ["will-navigate", "will-redirect", "will-frame-navigate"]) {
      assert.equal(fire(event, "http://127.0.0.1:5173").prevented, false);
      assert.equal(fire(event, "http://169.254.169.254").prevented, true);
      assert.equal(fire(event, "http://192.168.1.1").prevented, true);
    }
    const before = loaded.length;
    windowOpen("http://[::1]:5173");
    assert.equal(loaded.length, before + 1);
    windowOpen("file:///tmp/test");
    windowOpen("http://10.0.0.1");
    assert.equal(loaded.length, before + 1);
    eligible = false;
    assert.equal(fire("will-redirect", "http://localhost:5173").prevented, true);
    windowOpen("http://localhost:5173");
    assert.equal(loaded.length, before + 1);
    assert.equal(
      registry.openOrNavigate("unknown", "http://localhost:5173", undefined, {
        agent: true,
        agentContext: context,
      }).ok,
      false,
    );
    assert.equal(registry.has("unknown"), false);
  });

  it("does not carry a previous Arca context into another navigation or registry", () => {
    const context = {};
    const options = { allowsLocalhost: (ctx) => ctx === context };
    const first = makeEventCapturingRegistry(options);
    const second = makeEventCapturingRegistry();
    assert.equal(
      first.registry.openOrNavigate("same-id", "http://localhost", undefined, {
        agent: true,
        agentContext: context,
      }).ok,
      true,
    );
    assert.equal(
      second.registry.openOrNavigate("same-id", "http://localhost", undefined, {
        agent: true,
        agentContext: context,
      }).ok,
      false,
    );
    first.registry.openOrNavigate("same-id", "https://example.com", undefined, { agent: true });
    assert.equal(first.fire("will-redirect", "http://localhost").prevented, true);
    first.registry.openOrNavigate("same-id", "http://localhost", undefined, { force: true });
    assert.equal(first.fire("will-redirect", "http://10.0.0.1").prevented, false);
  });
  it("blocks a policy-enforced will-redirect to the cloud-metadata IP", () => {
    const { registry, sent, fire } = makeEventCapturingRegistry();
    // A registry call with opts.agent enables policy enforcement.
    registry.openOrNavigate("conv_1", "https://example.com/", undefined, { agent: true });
    // The server 302s to the metadata endpoint — the guard must cancel it.
    const ev = fire("will-redirect", "http://169.254.169.254/latest/meta-data/");
    assert.equal(ev.prevented, true, "will-redirect to metadata must be preventDefault'd");
    const blocked = sent.filter((s) => s.channel === "browser-nav-blocked");
    assert.equal(blocked.length, 1, "a browser-nav-blocked signal is emitted");
    assert.match(blocked[0].payload.error, /navigation blocked/);
  });

  it("blocks a policy-enforced will-navigate to a loopback host", () => {
    const { registry, fire } = makeEventCapturingRegistry();
    registry.openOrNavigate("conv_1", "https://example.com/", undefined, { agent: true });
    const ev = fire("will-navigate", "http://127.0.0.1:8080/");
    assert.equal(ev.prevented, true);
  });

  it("blocks a policy-enforced will-navigate to an RFC-1918 private host", () => {
    const { registry, fire } = makeEventCapturingRegistry();
    registry.openOrNavigate("conv_1", "https://example.com/", undefined, { agent: true });
    const ev = fire("will-navigate", "http://10.1.2.3/admin");
    assert.equal(ev.prevented, true);
  });

  it("allows a policy-enforced redirect between normal public https hosts", () => {
    const { registry, sent, fire } = makeEventCapturingRegistry();
    registry.openOrNavigate("conv_1", "https://example.com/", undefined, { agent: true });
    const ev = fire("will-redirect", "https://cdn.example.net/asset");
    assert.equal(ev.prevented, false, "a normal https→https redirect must not be blocked");
    assert.equal(sent.filter((s) => s.channel === "browser-nav-blocked").length, 0);
  });

  it("does NOT block redirects for a direct registry navigation without opts.agent", () => {
    const { registry, fire } = makeEventCapturingRegistry();
    registry.openOrNavigate("conv_1", "https://intranet.example.com/", undefined, {
      force: true,
    });
    const ev = fire("will-redirect", "http://10.0.0.5/sso/callback");
    assert.equal(
      ev.prevented,
      false,
      "direct registry navigation without opts.agent leaves enforcement off",
    );
  });

  it("a direct registry navigation without opts.agent clears prior enforcement", () => {
    const { registry, fire } = makeEventCapturingRegistry();
    registry.openOrNavigate("conv_1", "https://example.com/", undefined, { agent: true });
    registry.openOrNavigate("conv_1", "https://intranet.example.com/", undefined, {
      force: true,
    });
    const ev = fire("will-redirect", "http://192.168.1.10/");
    assert.equal(ev.prevented, false, "the later direct registry navigation cleared enforcement");
  });
});

describe("browserViewRegistry — per-server agent allowlist", () => {
  const aContext = { serverOrigin: "https://a.example" };
  const allowlistForA = (ctx) =>
    ctx?.serverOrigin === "https://a.example" ? [privateIpv4Address(192, 168, 1, 20)] : [];

  it("applies the context's allowlist on the first hop and reports the grant on refusal", () => {
    const { registry, loaded } = makeEventCapturingRegistry({ getAgentAllowlist: allowlistForA });
    const allowed = registry.openOrNavigate(
      "conv_1",
      `http://${privateIpv4Address(192, 168, 1, 20)}/`,
      undefined,
      {
        agent: true,
        agentContext: aContext,
      },
    );
    assert.equal(allowed.ok, true);
    assert.deepEqual(loaded, [`http://${privateIpv4Address(192, 168, 1, 20)}/`]);

    const refused = registry.openOrNavigate(
      "conv_2",
      `http://${privateIpv4Address(192, 168, 1, 20)}/`,
      undefined,
      {
        agent: true,
        agentContext: { serverOrigin: "https://b.example" },
      },
    );
    assert.equal(refused.ok, false);
    assert.equal(refused.grant, `${privateIpv4Address(192, 168, 1, 20)}:80`);
    assert.equal(registry.has("conv_2"), false, "a refused nav creates no view");
    assert.deepEqual(loaded, [`http://${privateIpv4Address(192, 168, 1, 20)}/`]);
  });

  it("uses the same list on redirects and in-place window-open navigations", () => {
    const { registry, fire, windowOpen, loaded, sent } = makeEventCapturingRegistry({
      getAgentAllowlist: allowlistForA,
    });
    registry.openOrNavigate("conv_1", `http://${privateIpv4Address(192, 168, 1, 20)}/`, undefined, {
      agent: true,
      agentContext: aContext,
    });
    assert.equal(
      fire("will-redirect", `http://${privateIpv4Address(192, 168, 1, 20)}/next`).prevented,
      false,
    );
    assert.equal(
      fire("will-redirect", `http://${privateIpv4Address(192, 168, 1, 21)}/`).prevented,
      true,
    );
    assert.equal(
      fire("will-navigate", `http://${privateIpv4Address(192, 168, 1, 21)}/`).prevented,
      true,
    );
    assert.equal(sent.filter((s) => s.channel === "browser-nav-blocked").length, 2);

    loaded.length = 0;
    assert.deepEqual(windowOpen(`http://${privateIpv4Address(192, 168, 1, 20)}/listed`), {
      action: "deny",
    });
    assert.deepEqual(loaded, [`http://${privateIpv4Address(192, 168, 1, 20)}/listed`]);
    windowOpen(`http://${privateIpv4Address(192, 168, 1, 21)}/unlisted`);
    assert.deepEqual(
      loaded,
      [`http://${privateIpv4Address(192, 168, 1, 20)}/listed`],
      "unlisted target never loads",
    );
  });

  it("scopes an allowOnce grant to one agent navigation and its redirects", () => {
    const { registry, fire } = makeEventCapturingRegistry();
    const once = registry.openOrNavigate(
      "conv_1",
      `http://${privateIpv4Address(192, 168, 1, 20)}/`,
      undefined,
      {
        agent: true,
        agentContext: {
          serverOrigin: "https://b.example",
          allowOnce: [`${privateIpv4Address(192, 168, 1, 20)}:80`],
        },
      },
    );
    assert.equal(once.ok, true);
    assert.equal(
      fire("will-redirect", `http://${privateIpv4Address(192, 168, 1, 20)}/login`).prevented,
      false,
    );

    // The next agent navigation replaces the context — the once-grant is gone.
    registry.openOrNavigate("conv_1", "https://example.com/", undefined, {
      agent: true,
      agentContext: { serverOrigin: "https://b.example" },
    });
    assert.equal(
      fire("will-navigate", `http://${privateIpv4Address(192, 168, 1, 20)}/`).prevented,
      true,
    );
  });

  it("leaves user navigation permissive and treats a non-array allowlist as empty", () => {
    const { registry, fire, loaded } = makeEventCapturingRegistry({
      getAgentAllowlist: () => privateIpv4Address(192, 168, 1, 20),
    });
    const user = registry.openOrNavigate(
      "conv_1",
      `http://${privateIpv4Address(192, 168, 1, 21)}/`,
      undefined,
      {
        force: true,
      },
    );
    assert.equal(user.ok, true);
    assert.deepEqual(loaded, [`http://${privateIpv4Address(192, 168, 1, 21)}/`]);
    assert.equal(
      fire("will-redirect", `http://${privateIpv4Address(192, 168, 1, 21)}/next`).prevented,
      false,
    );

    const agent = registry.openOrNavigate(
      "conv_2",
      `http://${privateIpv4Address(192, 168, 1, 20)}/`,
      undefined,
      {
        agent: true,
        agentContext: {},
      },
    );
    assert.equal(agent.ok, false, "a non-array allowlist must not open the policy");
  });
});

// DNS and proxy stubs for the network-layer guard; lookups record their
// partition argument.
function makeNetworkGuardRegistry({ proxy = "DIRECT", ...extra } = {}) {
  const lookups = [];
  const dnsTable = {
    "lvh.me": ["127.0.0.1"],
    "localtest.me": ["127.0.0.1"],
    "127.0.0.1.nip.io": ["127.0.0.1"],
    "169-254-169-254.nip.io": ["169.254.169.254"],
    "rebind.example": ["93.184.215.14"],
    "example.com": ["93.184.215.14"],
    "cdn.example.net": ["93.184.215.15"],
    "proxy.corp": ["10.0.0.2"],
  };
  const ctx = makeEventCapturingRegistry({
    partitionScope: "w-test",
    resolveHost: async (hostname, partition) => {
      lookups.push({ hostname, partition });
      return dnsTable[hostname] ?? [];
    },
    resolveProxy: async () => proxy,
    ...extra,
  });
  const idOf = (viewId) => ctx.registry.get(viewId).view.webContents.id;
  return { ...ctx, lookups, idOf };
}

describe("browserViewRegistry — network-layer agent guard (DNS + connected address)", () => {
  it("does not apply a tab policy to unowned worker requests", async () => {
    const { registry, lookups } = makeNetworkGuardRegistry();
    registry.openOrNavigate("conv_1", "https://example.com/", undefined, { agent: true });
    const partition = agentPartition("w-test", "conv_1");
    const request = {
      webContentsId: 0,
      url: "http://lvh.me/worker.js",
      resourceType: "script",
      ip: "127.0.0.1",
    };
    assert.equal(await registry.shouldCancelAgentRequest(request, partition), false);
    assert.equal(await registry.shouldCancelAgentResponse(request, partition), false);
    assert.deepEqual(
      registry.agentRequestHeaders({ ...request, requestHeaders: {} }, partition),
      {},
    );
    assert.deepEqual(lookups, []);
  });

  it("keeps an address-based localhost allowance through every navigation guard", async () => {
    const { registry, idOf } = makeNetworkGuardRegistry({
      resolveHost: async () => ["127.0.0.1"],
      getBrowserAccess: () => ({ level: "agent", allowed: ["127.0.0.0/8"] }),
    });
    const url = "http://localhost:8080/";
    assert.equal((await registry.checkAgentNavigation("conv_1", url, {})).ok, true);
    assert.equal(
      registry.openOrNavigate("conv_1", url, undefined, { agent: true, agentContext: {} }).ok,
      true,
    );
    const webContentsId = idOf("conv_1");
    assert.equal(await registry.shouldCancelAgentRequest({ webContentsId, url }), false);
    assert.equal(
      await registry.shouldCancelAgentResponse({ webContentsId, url, ip: "127.0.0.1" }),
      false,
    );
  });
  for (const once of [false, true]) {
    it(`keeps a ${once ? "once" : "configured"} grant through DNS and response guards`, async () => {
      const grant = "lvh.me:8899";
      const context = {
        serverOrigin: "https://server.example",
        ...(once ? { allowOnce: [grant] } : {}),
      };
      const { registry, idOf } = makeNetworkGuardRegistry({
        getAgentAllowlist: () => (once ? [] : [grant]),
      });
      const url = "http://lvh.me:8899/";
      assert.equal((await registry.checkAgentNavigation("conv_1", url, context)).ok, true);
      registry.openOrNavigate("conv_1", url, undefined, { agent: true, agentContext: context });
      const webContentsId = idOf("conv_1");
      assert.equal(await registry.shouldCancelAgentRequest({ webContentsId, url }), false);
      assert.equal(
        await registry.shouldCancelAgentResponse({ webContentsId, url, ip: "127.0.0.1" }),
        false,
      );
      assert.equal(
        await registry.shouldCancelAgentResponse({ webContentsId, url, ip: "169.254.169.254" }),
        true,
      );
    });
  }

  it("scopes loopback document access to the eligible view, including frames", async () => {
    const { registry, idOf, fire } = makeNetworkGuardRegistry({
      allowsLocalhost: (context) => context?.hostIsThisMachine === true,
    });
    registry.openOrNavigate("local", "http://127.0.0.1:5173", undefined, {
      agent: true,
      agentContext: { hostIsThisMachine: true },
    });
    assert.equal(fire("will-redirect", "http://192.168.1.1").prevented, true);
    registry.openOrNavigate("local", "https://example.com", undefined, {
      agent: true,
      agentContext: { hostIsThisMachine: true },
    });
    const frame = {
      webContentsId: idOf("local"),
      url: "http://127.0.0.1:5173",
      resourceType: "subFrame",
      ip: "127.0.0.1",
    };
    assert.equal(await registry.shouldCancelAgentRequest(frame), false);
    assert.equal(await registry.shouldCancelAgentResponse(frame), false);
    assert.equal(fire("will-frame-navigate", frame.url).prevented, false);
    registry.openOrNavigate("remote", "https://example.com", undefined, { agent: true });
    assert.equal(
      await registry.shouldCancelAgentRequest({ ...frame, webContentsId: idOf("remote") }),
      true,
    );
  });

  it("revokes loopback document access before the blank page commits", async () => {
    const { registry, idOf } = makeNetworkGuardRegistry({
      allowsLocalhost: (context) => context?.hostIsThisMachine === true,
    });
    const context = { hostIsThisMachine: true };
    registry.openOrNavigate("local", "https://example.com/", undefined, {
      agent: true,
      agentContext: context,
    });
    const entry = registry.get("local");
    let commitBlank;
    entry.view.webContents.loadURL = (url) => {
      assert.equal(url, "about:blank");
      return new Promise((resolve) => {
        commitBlank = resolve;
      });
    };
    const revocation = registry.revokeAgentView("local");
    assert.equal(context.revoked, true);
    assert.equal(entry.agentContext, context, "blank navigation is still pending");
    const partition = agentPartition("w-test", "local");
    try {
      await Promise.all(
        [idOf("local")].flatMap((webContentsId) =>
          ["mainFrame", "subFrame", "object"].map(async (resourceType) => {
            const request = {
              webContentsId,
              resourceType,
              url: `${resourceType === "webSocket" ? "ws" : "http"}://127.0.0.1:5173/x`,
            };
            assert.equal(await registry.shouldCancelAgentRequest(request, partition), true);
            const response =
              resourceType === "webSocket"
                ? { ...request, statusCode: 101 }
                : { ...request, ip: "127.0.0.1" };
            assert.equal(await registry.shouldCancelAgentResponse(response, partition), true);
          }),
        ),
      );
      const publicWorker = {
        resourceType: "xhr",
        url: "https://example.com/asset",
        ip: "93.184.215.14",
      };
      assert.equal(await registry.shouldCancelAgentRequest(publicWorker, partition), false);
      assert.equal(await registry.shouldCancelAgentResponse(publicWorker, partition), false);
    } finally {
      commitBlank();
      assert.deepEqual(await revocation, { ok: true });
    }
    assert.equal(entry.agentContext, null);
  });

  it("refuses an agent first hop to a name that resolves to loopback", async () => {
    const { registry, lookups } = makeNetworkGuardRegistry();
    const verdict = await registry.checkAgentNavigation("conv_1", "http://lvh.me:8899/", null);
    assert.equal(verdict.ok, false);
    assert.match(verdict.error, /does not resolve to a public address/);
    assert.deepEqual(lookups, [
      { hostname: "lvh.me", partition: agentPartition("w-test", "conv_1") },
    ]);
    assert.equal(registry.has("conv_1"), false, "the check creates no view");
  });

  it("cancels a policy-enforced redirect or frame request to a loopback alias", async () => {
    const { registry, sent, idOf } = makeNetworkGuardRegistry();
    registry.openOrNavigate("conv_1", "https://example.com/", undefined, { agent: true });
    const webContentsId = idOf("conv_1");
    assert.equal(
      await registry.shouldCancelAgentRequest({ webContentsId, url: "https://example.com/" }),
      false,
    );
    assert.equal(
      await registry.shouldCancelAgentRequest({ webContentsId, url: "https://cdn.example.net/x" }),
      false,
    );
    assert.equal(
      await registry.shouldCancelAgentRequest({ webContentsId, url: "http://lvh.me:8899/" }),
      true,
    );
    const blocked = sent.filter((s) => s.channel === "browser-nav-blocked");
    assert.equal(blocked.length, 1);
    assert.equal(blocked[0].payload.conversationId, "conv_1");
  });

  it("guards covered document types with DNS and connected-IP rules", async () => {
    const { registry, lookups, idOf } = makeNetworkGuardRegistry();
    const entry = registry.openOrNavigate("conv_1", "https://example.com/", undefined, {
      agent: true,
    }).entry;
    entry.view.webContents.close = () => assert.fail("network enforcement must keep the view");
    const webContentsId = idOf("conv_1");
    const resourceTypes = ["mainFrame", "subFrame", "object"];
    const request = { webContentsId, url: "https://example.com/x.js", resourceType: "xhr" };
    await Promise.all(
      resourceTypes.map(async (resourceType) => {
        const smoke = { ...request, resourceType };
        assert.equal(await registry.shouldCancelAgentRequest(smoke), false, resourceType);
        assert.equal(
          await registry.shouldCancelAgentResponse({ ...smoke, ip: "127.0.0.1" }),
          true,
          `${resourceType}: private connected IP`,
        );
      }),
    );
    await Promise.all(
      [
        ["http://127.0.0.1:8899/x", true],
        ["http://192.168.1.1/x", true],
        ["http://169.254.169.254/x", true],
        ["ws://localhost/live", true],
        ["https://93.184.215.14/x", false],
      ].map(async ([url, cancel]) => {
        assert.equal(await registry.shouldCancelAgentRequest({ ...request, url }), cancel, url);
      }),
    );
    await Promise.all(
      ["93.184.215.14", "127.0.0.1", "192.168.1.1", "169.254.169.254", undefined].map(
        async (ip) => {
          assert.equal(
            await registry.shouldCancelAgentResponse({ ...request, ip }),
            ip !== "93.184.215.14",
            `connected IP: ${ip}`,
          );
        },
      ),
    );
    assert.deepEqual(
      lookups,
      resourceTypes.map(() => ({
        hostname: "example.com",
        partition: agentPartition("w-test", "conv_1"),
      })),
      "named documents resolve through their owning partition; IP literals skip DNS",
    );
    assert.equal(registry.get("conv_1"), entry);
  });

  it("preflights document methods to private-resolving names in the owning partition", async () => {
    const { registry, lookups, idOf } = makeNetworkGuardRegistry();
    registry.openOrNavigate("conv_1", "https://example.com/", undefined, { agent: true });
    const aliases = ["lvh.me", "localtest.me", "127.0.0.1.nip.io", "169-254-169-254.nip.io"];
    const methods = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"];
    const requests = [
      ...methods.map((method) => ({ hostname: "localtest.me", method, resourceType: "mainFrame" })),
      ...aliases
        .filter((hostname) => hostname !== "localtest.me")
        .map((hostname) => ({
          hostname,
          method: "GET",
          resourceType: "mainFrame",
        })),
      { hostname: "localtest.me", method: "POST", resourceType: "object" },
      { hostname: "lvh.me", method: "GET", resourceType: "subFrame" },
    ];
    await Promise.all(
      requests.map(async ({ hostname, method, resourceType }) => {
        const url = `${resourceType === "webSocket" ? "ws" : "http"}://${hostname}/request`;
        assert.equal(
          await registry.shouldCancelAgentRequest({
            webContentsId: idOf("conv_1"),
            url,
            method,
            resourceType,
          }),
          true,
          `${method} ${resourceType} ${hostname}`,
        );
      }),
    );
    assert.deepEqual(
      lookups.map((lookup) => lookup.hostname).sort(),
      requests.map((request) => request.hostname).sort(),
    );
    assert.ok(lookups.every((lookup) => lookup.partition === agentPartition("w-test", "conv_1")));
    assert.equal(
      await registry.shouldCancelAgentRequest({
        webContentsId: idOf("conv_1"),
        url: "https://example.com/post",
        method: "POST",
        resourceType: "mainFrame",
      }),
      false,
      "public unsafe methods remain available",
    );
  });

  it("rechecks DNS for WebSocket upgrades without an IP while missing HTTP endpoints fail closed", async () => {
    let addresses = ["93.184.215.14"];
    const resolvedPartitions = [];
    const { registry, idOf } = makeNetworkGuardRegistry({
      resolveHost: async (_hostname, partition) => {
        resolvedPartitions.push(partition);
        return addresses;
      },
    });
    registry.openOrNavigate("conv_1", "https://example.com/", undefined, { agent: true });
    const socket = {
      webContentsId: idOf("conv_1"),
      url: "wss://example.com/live",
      resourceType: "webSocket",
      statusCode: 101,
    };
    assert.equal(
      await registry.shouldCancelAgentRequest(socket),
      false,
      "public WebSocket preflight",
    );
    assert.equal(
      await registry.shouldCancelAgentResponse(socket),
      false,
      "a public upgrade can omit its IP",
    );
    assert.equal(resolvedPartitions.length, 2, "DNS is checked before and after the handshake");
    addresses = ["127.0.0.1"];
    assert.equal(
      await registry.shouldCancelAgentResponse(socket),
      true,
      "rebinding seen by the response resolver is rejected",
    );
    assert.ok(
      resolvedPartitions.every((partition) => partition === agentPartition("w-test", "conv_1")),
    );
    addresses = ["169.254.169.254"];
    assert.equal(
      await registry.shouldCancelAgentResponse({
        ...socket,
        url: "ws://169-254-169-254.nip.io/live",
      }),
      true,
      "a public-only view refuses metadata-resolving WebSocket upgrades",
    );
    addresses = ["192.168.1.1"];
    assert.equal(
      await registry.shouldCancelAgentResponse(socket),
      true,
      "LAN-resolving upgrades are rejected",
    );
    addresses = ["93.184.215.14"];
    assert.equal(
      await registry.shouldCancelAgentResponse({ ...socket, ip: "127.0.0.1" }),
      true,
      "an available private endpoint is still rejected",
    );
    assert.equal(
      await registry.shouldCancelAgentResponse({ ...socket, resourceType: "xhr" }),
      true,
      "HTTP responses still need an IP",
    );
    const beforeLiterals = resolvedPartitions.length;
    await Promise.all(
      ["127.0.0.1", "192.168.1.1", "169.254.169.254"].map(async (host) => {
        assert.equal(
          await registry.shouldCancelAgentResponse({ ...socket, url: `ws://${host}/live` }),
          true,
          `public-only WebSocket literal ${host}`,
        );
      }),
    );
    assert.equal(resolvedPartitions.length, beforeLiterals, "private literals fail before DNS");
    assert.equal(
      await registry.shouldCancelAgentResponse({ ...socket, statusCode: 200 }),
      true,
      "only an actual upgrade uses the DNS fallback",
    );
  });

  it("cancels an agent response whose connected address is internal or missing", async () => {
    const { registry, sent, idOf } = makeNetworkGuardRegistry();
    registry.openOrNavigate("conv_1", "http://rebind.example/", undefined, { agent: true });
    const response = (connectedAddress) =>
      registry.shouldCancelAgentResponse({
        webContentsId: idOf("conv_1"),
        url: "http://rebind.example/",
        ip: connectedAddress,
      });
    // A DNS answer that changed to loopback after the request check is still
    // refused, as is a response with no connected address to judge.
    const cancels = await Promise.all(["93.184.215.14", "127.0.0.1", undefined].map(response));
    assert.deepEqual(cancels, [false, true, true]);
    assert.equal(sent.filter((s) => s.channel === "browser-nav-blocked").length, 2);
  });

  it("exempts an internal connected address only when it is the configured proxy's IP", async () => {
    const proxyLookups = [];
    const { registry, idOf, lookups } = makeNetworkGuardRegistry({
      resolveProxy: async (url, partition) => {
        proxyLookups.push({ url, partition });
        return "PROXY proxy.corp:3128; DIRECT";
      },
    });
    registry.openOrNavigate("conv_1", "http://rebind.example/", undefined, { agent: true });
    const response = (connectedAddress) =>
      registry.shouldCancelAgentResponse({
        webContentsId: idOf("conv_1"),
        url: "http://rebind.example/",
        ip: connectedAddress,
      });
    // The endpoint metadata cannot distinguish this direct connection from the configured proxy.
    assert.deepEqual(await Promise.all(["10.0.0.2", "127.0.0.1"].map(response)), [false, true]);
    const partition = agentPartition("w-test", "conv_1");
    const socket = {
      webContentsId: idOf("conv_1"),
      url: "wss://rebind.example/live",
      resourceType: "webSocket",
      statusCode: 101,
      ip: "10.0.0.2",
    };
    assert.equal(await registry.shouldCancelAgentResponse(socket, partition), false);
    assert.deepEqual(proxyLookups, [
      { url: "http://rebind.example/", partition },
      { url: "http://rebind.example/", partition },
      { url: "https://rebind.example/live", partition },
    ]);
    assert.deepEqual(
      lookups,
      Array.from({ length: 3 }, () => ({ hostname: "proxy.corp", partition })),
    );
  });

  it("accepts a *.localhost response only from loopback on a direct route", async () => {
    const context = { hostIsThisMachine: true };
    const responsesOn = async (route, ips) => {
      const proxyLookups = [];
      const { registry, idOf } = makeNetworkGuardRegistry({
        allowsLocalhost: (ctx) => ctx === context,
        resolveProxy: async (url) => {
          proxyLookups.push(url);
          return route;
        },
      });
      const url = "http://spog.dev.localhost:5173/";
      registry.openOrNavigate("conv_1", url, undefined, { agent: true, agentContext: context });
      const webContentsId = idOf("conv_1");
      const cancels = await Promise.all(
        ips.map((ip) => registry.shouldCancelAgentResponse({ webContentsId, url, ip })),
      );
      return { cancels, proxyLookups };
    };
    const direct = await responsesOn("DIRECT", ["127.0.0.1", "::1", "10.0.0.2", "93.184.215.14"]);
    assert.deepEqual(direct.cancels, [false, false, true, true]);
    // Only loopback responses reach the route check.
    assert.deepEqual(direct.proxyLookups, [
      "http://spog.dev.localhost:5173/",
      "http://spog.dev.localhost:5173/",
    ]);
    // A local proxy answers from loopback but resolves the name on its own network.
    const localProxy = await responsesOn("PROXY 127.0.0.1:3128", ["127.0.0.1"]);
    assert.deepEqual(localProxy.cancels, [true]);
    // A remote proxy's address gets no proxy exemption for *.localhost.
    const remoteProxy = await responsesOn("PROXY proxy.corp:3128; DIRECT", ["10.0.0.2"]);
    assert.deepEqual(remoteProxy.cancels, [true]);
  });

  it("sends *.localhost requests and IP-less upgrades only on a direct route for an eligible view", async () => {
    const context = { hostIsThisMachine: true };
    const verdictsOn = async (resolveProxy, viewId = "local") => {
      const { registry, idOf, lookups } = makeNetworkGuardRegistry({
        allowsLocalhost: (ctx) => ctx === context,
        resolveProxy,
      });
      registry.openOrNavigate("local", "http://spog.dev.localhost:5173/", undefined, {
        agent: true,
        agentContext: context,
      });
      registry.openOrNavigate("remote", "https://example.com/", undefined, { agent: true });
      const webContentsId = idOf(viewId);
      const page = { webContentsId, url: "http://spog.dev.localhost:5173/", resourceType: "xhr" };
      const socket = { webContentsId, url: "ws://spog.dev.localhost:5173/hmr" };
      const worker = { ...page, webContentsId: undefined };
      const verdicts = {
        request: await registry.shouldCancelAgentRequest(page),
        workerRequest: await registry.shouldCancelAgentRequest(
          worker,
          agentPartition("w-test", viewId),
        ),
        socketRequest: await registry.shouldCancelAgentRequest(socket),
        upgrade: await registry.shouldCancelAgentResponse({
          ...socket,
          resourceType: "webSocket",
          statusCode: 101,
        }),
        exactLocalhost: await registry.shouldCancelAgentRequest({
          ...page,
          url: "http://localhost:5173/",
        }),
      };
      assert.deepEqual(lookups, [], "no DNS lookup for *.localhost");
      return verdicts;
    };
    const directLookups = [];
    const direct = async (url) => {
      directLookups.push(url);
      return "DIRECT";
    };
    assert.deepEqual(
      await verdictsOn(direct),
      {
        request: false,
        workerRequest: false,
        socketRequest: false,
        upgrade: false,
        exactLocalhost: false,
      },
      "Chromium connects a direct *.localhost to loopback",
    );
    // WebSockets are looked up by their own scheme, which picks their proxy.
    assert.deepEqual(directLookups, [
      "http://spog.dev.localhost:5173/",
      "ws://spog.dev.localhost:5173/hmr",
      "ws://spog.dev.localhost:5173/hmr",
    ]);
    // An `https=`-only proxy rule carries WebSockets but not plain HTTP.
    assert.deepEqual(
      await verdictsOn(async (url) =>
        new URL(url).protocol === "ws:" ? "PROXY 127.0.0.1:3128" : "DIRECT",
      ),
      {
        request: false,
        workerRequest: false,
        socketRequest: true,
        upgrade: true,
        exactLocalhost: false,
      },
    );
    assert.deepEqual(
      await verdictsOn(direct, "remote"),
      {
        request: true,
        workerRequest: false,
        socketRequest: true,
        upgrade: true,
        exactLocalhost: true,
      },
      "an ineligible view never opens *.localhost",
    );
    const unsafeRoutes = {
      empty: async () => "",
      "proxy fallback": async () => "DIRECT; PROXY proxy.corp:3128",
      unparsable: async () => "BOGUS",
      "lookup failure": async () => {
        throw new Error("no route");
      },
    };
    const verdicts = await Promise.all(
      Object.entries(unsafeRoutes).map(async ([label, route]) => [label, await verdictsOn(route)]),
    );
    // Exact localhost keeps its existing request-stage behaviour.
    const refused = {
      request: true,
      workerRequest: false,
      socketRequest: true,
      upgrade: true,
      exactLocalhost: false,
    };
    assert.deepEqual(
      verdicts,
      Object.keys(unsafeRoutes).map((label) => [label, refused]),
    );
  });

  it("forces policy-enforced network requests past the HTTP cache", () => {
    const { registry, idOf } = makeNetworkGuardRegistry();
    registry.openOrNavigate("conv_1", "https://example.com/", undefined, { agent: true });
    registry.openOrNavigate("browser-tab:conv_1:t1", "https://example.com/");
    const requestHeaders = { Accept: "text/html", "cache-control": "max-age=0" };
    const agentHeaders = (headers) =>
      registry.agentRequestHeaders({ webContentsId: idOf("conv_1"), requestHeaders: headers })
        .requestHeaders;
    // Existing directives are kept; no-cache is added once, matched case-insensitively.
    assert.deepEqual(agentHeaders(requestHeaders), {
      Accept: "text/html",
      "Cache-Control": "max-age=0, no-cache",
    });
    assert.deepEqual(agentHeaders({ "Cache-Control": "no-store" }), {
      "Cache-Control": "no-store, no-cache",
    });
    assert.deepEqual(agentHeaders({ "cache-control": "No-Cache, no-store" }), {
      "Cache-Control": "No-Cache, no-store",
    });
    assert.deepEqual(
      registry.agentRequestHeaders({
        webContentsId: idOf("browser-tab:conv_1:t1"),
        requestHeaders,
      }),
      {},
      "the direct registry tab without opts.agent keeps normal caching",
    );
  });

  it("leaves direct unenforced views and unattributed requests without a partition alone", async () => {
    const { registry, lookups, idOf } = makeNetworkGuardRegistry();
    registry.openOrNavigate("conv_1", "http://intranet.example/", undefined, { force: true });
    const webContentsId = idOf("conv_1");
    const partition = agentPartition("w-test", "conv_1");
    const request = { webContentsId, url: "http://lvh.me:8899/", ip: "127.0.0.1" };
    assert.equal(await registry.shouldCancelAgentRequest(request, partition), false);
    assert.equal(await registry.shouldCancelAgentResponse(request, partition), false);
    const stray = { ...request, webContentsId: 999 };
    assert.equal(await registry.shouldCancelAgentRequest(stray), false);
    assert.equal(await registry.shouldCancelAgentResponse(stray), false);
    assert.deepEqual(lookups, []);
  });

  it("keeps the Arca loopback exception for exact hosts but not DNS aliases", async () => {
    const context = { sourceHostId: "arca-host" };
    const { registry, idOf } = makeNetworkGuardRegistry({
      allowsLocalhost: (ctx) => ctx === context,
    });
    registry.openOrNavigate("arca", "http://localhost:5173/", undefined, {
      agent: true,
      agentContext: context,
    });
    const webContentsId = idOf("arca");
    await Promise.all(
      [
        ["http://localhost:5173/", "127.0.0.1"],
        ["http://127.0.0.1:5173/", "127.0.0.1"],
        ["http://[::1]:5173/", "::1"],
        ["ws://localhost:5173/live", "127.0.0.1"],
        ["wss://[::1]:5173/live", "::1"],
      ].map(async ([url, ip]) => {
        const request = { webContentsId, url, ip, resourceType: "other" };
        assert.equal(await registry.shouldCancelAgentRequest(request), false, url);
        assert.equal(await registry.shouldCancelAgentResponse(request), false, url);
      }),
    );
    const alias = { webContentsId, url: "http://lvh.me:5173/", ip: "127.0.0.1" };
    assert.equal(await registry.shouldCancelAgentRequest(alias), true);
    assert.equal(await registry.shouldCancelAgentResponse(alias), true);
  });
});

describe("browserViewRegistry — child window.open is denied (S3)", () => {
  it("never opens a window: every window.open target is answered with deny", () => {
    const { registry, windowOpen } = makeEventCapturingRegistry();
    registry.openOrNavigate("conv_1", "https://example.com/", undefined, { agent: true });
    assert.deepEqual(windowOpen("https://evil.example.com/popup"), { action: "deny" });
    assert.deepEqual(windowOpen("https://example.com/ok"), { action: "deny" });
  });

  it("navigates the SAME view in place for an http(s) link target", () => {
    const { registry, windowOpen, loaded } = makeEventCapturingRegistry();
    registry.openOrNavigate("conv_1", "https://example.com/", undefined, { agent: true });
    loaded.length = 0; // ignore the initial openOrNavigate load
    assert.deepEqual(windowOpen("https://example.com/linked-page"), { action: "deny" });
    assert.deepEqual(loaded, ["https://example.com/linked-page"]);
  });

  it("blocks a policy-enforced window.open to an internal host (no in-place nav)", () => {
    const { registry, windowOpen, loaded, sent } = makeEventCapturingRegistry();
    registry.openOrNavigate("conv_1", "https://example.com/", undefined, { agent: true });
    loaded.length = 0;
    assert.deepEqual(windowOpen("http://169.254.169.254/latest/meta-data/"), {
      action: "deny",
    });
    assert.deepEqual(loaded, [], "no in-place navigation to a blocked host");
    const blocked = sent.filter((s) => s.channel === "browser-nav-blocked");
    assert.equal(blocked.length, 1, "the SSRF block surfaces to the renderer");
  });

  it("still denies non-web schemes with no in-place nav", () => {
    const { registry, windowOpen, loaded } = makeEventCapturingRegistry();
    registry.openOrNavigate("conv_1", "https://example.com/", undefined, { agent: true });
    loaded.length = 0;
    assert.deepEqual(windowOpen("file:///etc/passwd"), { action: "deny" });
    assert.deepEqual(windowOpen("javascript:alert(1)"), { action: "deny" });
    assert.deepEqual(loaded, []);
  });
});

describe("browserViewRegistry — pane context menu", () => {
  it("offers Open Link in Browser + Copy Link Address over a link", () => {
    const { registry, fireContextMenu, menus, externalOpens, clipboardWrites } =
      makeEventCapturingRegistry();
    registry.openOrNavigate("conv_1", "https://example.com/", undefined, { agent: true });
    fireContextMenu({ linkURL: "https://example.com/page", selectionText: "" });
    assert.equal(menus.length, 1);
    assert.deepEqual(
      menus[0].map((item) => item.label),
      ["Open Link in Browser", "Copy Link Address"],
    );
    menus[0][0].click();
    menus[0][1].click();
    assert.deepEqual(externalOpens, ["https://example.com/page"]);
    assert.deepEqual(clipboardWrites, ["https://example.com/page"]);
  });

  it("omits Open Link in Browser for a non-web link but still offers Copy Link Address", () => {
    const { registry, fireContextMenu, menus } = makeEventCapturingRegistry();
    registry.openOrNavigate("conv_1", "https://example.com/", undefined, { agent: true });
    fireContextMenu({ linkURL: "javascript:alert(1)", selectionText: "" });
    assert.deepEqual(
      menus[0].map((item) => item.label),
      ["Copy Link Address"],
    );
  });

  it("offers Copy over selected text and routes it to webContents.copy()", () => {
    const { registry, fireContextMenu, menus, copyCalls } = makeEventCapturingRegistry();
    registry.openOrNavigate("conv_1", "https://example.com/", undefined, { agent: true });
    fireContextMenu({ linkURL: "", selectionText: "hello" });
    assert.deepEqual(
      menus[0].map((item) => item.label),
      ["Copy"],
    );
    menus[0][0].click();
    assert.equal(copyCalls(), 1);
  });

  it("shows nothing over dead space", () => {
    const { registry, fireContextMenu, menus } = makeEventCapturingRegistry();
    registry.openOrNavigate("conv_1", "https://example.com/", undefined, { agent: true });
    fireContextMenu({ linkURL: "", selectionText: "   " });
    assert.equal(menus.length, 0);
  });
});

describe("browserViewRegistry — overlay suppression (#3980)", () => {
  let ctx;
  beforeEach(() => {
    ctx = makeRegistry();
  });

  it("toggles setVisible on the active view when suppressed/unsuppressed", () => {
    ctx.registry.openOrNavigate("conv_1", "https://example.com");
    ctx.registry.setActive("conv_1");
    ctx.visibility.length = 0; // ignore any visibility calls during attach
    ctx.registry.setSuppressed(true);
    assert.deepEqual(ctx.visibility, [false], "suppress hides the active view");
    ctx.registry.setSuppressed(false);
    assert.deepEqual(ctx.visibility, [false, true], "unsuppress shows it again");
    assert.equal(ctx.registry.isSuppressed(), false);
  });

  it("keeps a view hidden when it becomes active while suppression is on (sticky)", () => {
    // Suppress with nothing active, then attach a view — it must attach hidden.
    ctx.registry.openOrNavigate("conv_1", "https://example.com");
    ctx.registry.setSuppressed(true);
    ctx.visibility.length = 0;
    ctx.registry.setActive("conv_1");
    assert.ok(
      ctx.visibility.includes(false) && !ctx.visibility.includes(true),
      "a view attaching under active suppression stays hidden",
    );
  });

  it("is a no-op (no throw) when no view is active", () => {
    assert.deepEqual(ctx.registry.setSuppressed(true), { ok: true });
    assert.equal(ctx.registry.isSuppressed(), true);
    assert.equal(ctx.visibility.length, 0, "nothing to toggle with no active view");
  });
});

// A stub view that records device-emulation calls and the order of bounds /
// emulation events, and lets a test fire webContents lifecycle events.
function makeEmulationRegistry() {
  const makeStubView = () => {
    const handlers = new Map();
    const emulation = { enables: [], disables: 0, events: [] };
    return {
      emulation,
      fire(event) {
        const fn = handlers.get(event);
        if (fn) fn();
      },
      setBounds(bounds) {
        emulation.events.push({ type: "setBounds", width: bounds.width, height: bounds.height });
      },
      setVisible() {},
      webContents: {
        loadURL() {},
        close() {},
        removeListener() {},
        on(event, fn) {
          handlers.set(event, fn);
        },
        setWindowOpenHandler() {},
        isDestroyed: () => false,
        enableDeviceEmulation(opts) {
          emulation.enables.push(opts);
          emulation.events.push({ type: "enable" });
        },
        disableDeviceEmulation() {
          emulation.disables += 1;
          emulation.events.push({ type: "disable" });
        },
      },
    };
  };
  const registry = createBrowserViewRegistry({
    WebContentsViewCtor: () => makeStubView(),
    createBoundsController: createBrowserViewBoundsController,
    attachToHost() {},
    detachFromHost() {},
    sendToRenderer() {},
    getHostZoomFactor: () => 1,
  });
  return { registry };
}

describe("browserViewRegistry — hidden-view layout via device emulation", () => {
  it("leaves a detached view unemulated until its first commit, then lays it out at the fallback size", () => {
    const { registry } = makeEmulationRegistry();
    registry.openOrNavigate("conv_1", "https://example.com");
    const view = registry.get("conv_1").view;
    assert.deepEqual(view.emulation.enables, [], "no emulation before the first did-navigate");
    view.fire("did-navigate");
    assert.equal(view.emulation.enables.length, 1);
    assert.deepEqual(view.emulation.enables[0].viewSize, { width: 1280, height: 800 });
  });

  it("never emulates a never-navigated entry when it is detached or swapped away", () => {
    const { registry } = makeEmulationRegistry();
    registry.openOrNavigate("conv_1", "https://example.com");
    registry.openOrNavigate("conv_2", "https://example.com");
    const one = registry.get("conv_1").view;
    const two = registry.get("conv_2").view;

    registry.setActive("conv_1");
    registry.setActive(null);
    assert.equal(
      one.emulation.enables.length,
      0,
      "setActive(null) on an uncommitted view is a no-op",
    );

    registry.setActive("conv_1");
    registry.setActive("conv_2");
    assert.equal(one.emulation.enables.length, 0, "swapping away an uncommitted view is a no-op");
    assert.equal(two.emulation.enables.length, 0);
  });

  it("emulates a hidden view at its last on-screen size and disables before showing it again", () => {
    const { registry } = makeEmulationRegistry();
    registry.openOrNavigate("conv_1", "https://example.com");
    registry.openOrNavigate("conv_2", "https://example.com");
    const one = registry.get("conv_1").view;
    registry.setActive("conv_1");
    one.fire("did-navigate");
    registry.get("conv_1").boundsController.setRendererBounds({
      x: 10,
      y: 20,
      width: 640,
      height: 480,
      devicePixelRatio: 1,
    });
    assert.equal(one.emulation.enables.length, 0, "an active view needs no emulation");

    registry.setActive("conv_2");
    assert.equal(one.emulation.enables.length, 1);
    assert.deepEqual(one.emulation.enables[0].viewSize, { width: 640, height: 480 });

    one.emulation.events.length = 0;
    registry.setActive("conv_1");
    assert.deepEqual(
      one.emulation.events.map((e) => e.type),
      ["disable", "setBounds"],
      "re-activation disables emulation before the bounds resync",
    );

    registry.openOrNavigate("conv_3", "https://example.com");
    const three = registry.get("conv_3").view;
    three.fire("did-navigate");
    assert.equal(three.emulation.enables.length, 1);
    assert.deepEqual(three.emulation.enables[0].viewSize, { width: 640, height: 480 });
  });

  it("stops emulating after a renderer crash until the next commit", () => {
    const { registry } = makeEmulationRegistry();
    registry.openOrNavigate("conv_1", "https://example.com");
    const one = registry.get("conv_1").view;
    one.fire("did-navigate");
    assert.equal(one.emulation.enables.length, 1);

    one.fire("render-process-gone");
    registry.setActive("conv_1");
    registry.setActive(null);
    assert.equal(one.emulation.enables.length, 1, "no re-enable while uncommitted");
    assert.equal(one.emulation.disables, 0, "no disable call after the renderer died");

    one.fire("did-navigate");
    assert.equal(one.emulation.enables.length, 2);
  });

  it("does not emulate a view that commits while it is the active one", () => {
    const { registry } = makeEmulationRegistry();
    registry.openOrNavigate("conv_1", "https://example.com");
    registry.setActive("conv_1");
    const one = registry.get("conv_1").view;
    one.fire("did-navigate");
    assert.equal(one.emulation.enables.length, 0);
  });
});
