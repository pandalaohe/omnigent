import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  collectLocalUserPreferences,
  initializeUserPreferencesSync,
  queueUserPreferencePatch,
  refreshUserPreferencesFromServer,
  resetUserPreferencesSyncForTests,
  USER_PREFERENCES_PATCH_ACKNOWLEDGED_EVENT,
} from "./userPreferencesSync";
import {
  readApprovalTimeoutPreferences,
  writeApprovalTimeoutPreferences,
} from "./approvalTimeoutPreferences";
import { patchHostColor, readHostColorPreferences } from "./hostColorPreferences";
import { KEEP_WARM_STORAGE_KEY, writeKeepWarmPreferences } from "./keepWarmPreferences";

beforeEach(() => {
  localStorage.clear();
  resetUserPreferencesSyncForTests();
  vi.useRealTimers();
});

describe("user preference synchronization", () => {
  it("does nothing when an older Server omits the preferences field", async () => {
    localStorage.setItem("omnigent:context-indicator-mode", "compact");
    const fetcher = vi.fn();
    await initializeUserPreferencesSync(undefined, fetcher);
    expect(localStorage.getItem("omnigent:context-indicator-mode")).toBe("compact");
    expect(localStorage.getItem("omnigent:user-preferences-owner")).toBe("__default__:__local__");
    expect(fetcher).not.toHaveBeenCalled();
  });

  it("does not seed a new Server from an older Server's local preferences", async () => {
    localStorage.setItem("omnigent:context-indicator-mode", "compact");
    await initializeUserPreferencesSync(undefined, vi.fn(), "alice", "server-a");
    const serverB = vi.fn().mockResolvedValue(Response.json({ version: 1, settings: {} }));

    await initializeUserPreferencesSync(null, serverB, "alice", "server-b");

    expect(serverB).toHaveBeenCalledWith(
      "/v1/me/preferences",
      expect.objectContaining({ body: JSON.stringify({ version: 1, settings: {} }) }),
    );
    expect(localStorage.getItem("omnigent:context-indicator-mode")).toBeNull();
    expect(localStorage.getItem("omnigent:user-preferences-owner")).toBe("server-b:alice");
  });

  it("downgrades an unrecognized preferences envelope to device-only behavior", async () => {
    vi.useFakeTimers();
    const fetcher = vi.fn();
    await initializeUserPreferencesSync({ version: 2, settings: {} }, fetcher, "alice", "server-a");

    queueUserPreferencePatch("context_indicator", "compact");
    await vi.advanceTimersByTimeAsync(500);

    expect(fetcher).not.toHaveBeenCalled();
    expect(localStorage.getItem("omnigent:user-preferences-owner")).toBe("server-a:alice");
  });

  it("collects and hydrates the Agent badge namespace", async () => {
    const badgePreferences = {
      version: 1,
      enabled: false,
      entries: {
        "agent-a": { label: "A", borderColor: "#123456", textColor: "#abcdef" },
      },
    };
    localStorage.setItem("omnigent:agent-badge-preferences", JSON.stringify(badgePreferences));
    expect(collectLocalUserPreferences().settings.agent_badges).toEqual(badgePreferences);

    await initializeUserPreferencesSync(
      {
        version: 1,
        settings: { agent_badges: { ...badgePreferences, enabled: true } },
      },
      vi.fn(),
    );

    expect(JSON.parse(localStorage.getItem("omnigent:agent-badge-preferences") ?? "null")).toEqual({
      ...badgePreferences,
      enabled: true,
    });
  });

  it("collects, patches, and hydrates the Approval timeout namespace", async () => {
    vi.useFakeTimers();
    const fetcher = vi.fn().mockResolvedValue(new Response(null, { status: 200 }));
    await initializeUserPreferencesSync({ version: 1, settings: {} }, fetcher, "alice");

    const preferences = { timeoutMinutes: 10, stopTurn: false, asyncApprovals: false };
    writeApprovalTimeoutPreferences(preferences);
    await vi.advanceTimersByTimeAsync(251);
    expect(collectLocalUserPreferences().settings.approval_timeout).toEqual(preferences);
    expect(fetcher).toHaveBeenCalledWith(
      "/v1/me/preferences/approval_timeout",
      expect.objectContaining({
        method: "PATCH",
        body: JSON.stringify({ value: preferences }),
      }),
    );

    writeApprovalTimeoutPreferences({ timeoutMinutes: 50, stopTurn: true, asyncApprovals: true });
    await vi.advanceTimersByTimeAsync(500);
    expect(localStorage.getItem("omnigent:approval-timeout")).toBeNull();
    expect(fetcher).toHaveBeenLastCalledWith(
      "/v1/me/preferences/approval_timeout",
      expect.objectContaining({ body: JSON.stringify({ value: null }) }),
    );

    resetUserPreferencesSyncForTests();
    await initializeUserPreferencesSync(
      { version: 1, settings: { approval_timeout: { timeoutMinutes: 5, stopTurn: false } } },
      vi.fn(),
      "alice",
    );
    expect(readApprovalTimeoutPreferences()).toEqual({
      timeoutMinutes: 5,
      stopTurn: false,
      asyncApprovals: true,
    });
  });

  it("collects and hydrates the Agent pins namespace", async () => {
    const pins = { ids: ["ag_polly", "ca_reviewer"] };
    localStorage.setItem("omnigent:agent-pins", JSON.stringify(pins));
    expect(collectLocalUserPreferences().settings.agent_pins).toEqual(pins);

    await initializeUserPreferencesSync(
      { version: 1, settings: { agent_pins: { ids: ["ca_reviewer"] } } },
      vi.fn(),
    );

    expect(JSON.parse(localStorage.getItem("omnigent:agent-pins") ?? "null")).toEqual({
      ids: ["ca_reviewer"],
    });
  });

  it("collects and hydrates the calling-defaults namespaces", async () => {
    const callingDefaults = {
      "host-1": { codex: { model: "gpt-6-sol", effort: "high" } },
    };
    const callingLast = {
      enabled: true,
      "p:abc": { "host-1": { last_agent_id: "codex-sdk", agents: {} } },
    };
    localStorage.setItem("omnigent:calling-defaults", JSON.stringify(callingDefaults));
    localStorage.setItem("omnigent:calling-last", JSON.stringify(callingLast));
    expect(collectLocalUserPreferences().settings.calling_defaults).toEqual(callingDefaults);
    expect(collectLocalUserPreferences().settings.calling_last).toEqual(callingLast);

    await initializeUserPreferencesSync(
      {
        version: 1,
        settings: { calling_defaults: { "host-2": {} }, calling_last: { enabled: false } },
      },
      vi.fn(),
    );

    expect(JSON.parse(localStorage.getItem("omnigent:calling-defaults") ?? "null")).toEqual({
      "host-2": {},
    });
    expect(JSON.parse(localStorage.getItem("omnigent:calling-last") ?? "null")).toEqual({
      enabled: false,
    });
  });

  it("patches and hydrates the keep-warm namespace", async () => {
    vi.useFakeTimers();
    const fetcher = vi.fn().mockResolvedValue(new Response(null, { status: 200 }));
    await initializeUserPreferencesSync({ version: 1, settings: {} }, fetcher, "alice");

    const preferences = {
      agents: {
        "claude-native-ui": { main: true, child: true, intervalSeconds: 3300, maxSeconds: 14400 },
      },
      hostOfflineArchiveSeconds: 7200,
    };
    writeKeepWarmPreferences(preferences, [{ id: "claude-native-ui", harness: "claude-native" }]);
    await vi.advanceTimersByTimeAsync(251);

    expect(collectLocalUserPreferences().settings.keep_warm).toEqual(preferences);
    expect(fetcher.mock.calls.filter(([url]) => url === "/v1/me/preferences/keep_warm")).toEqual([
      [
        "/v1/me/preferences/keep_warm",
        expect.objectContaining({ method: "PATCH", body: JSON.stringify({ value: preferences }) }),
      ],
    ]);

    resetUserPreferencesSyncForTests();
    await initializeUserPreferencesSync(
      {
        version: 1,
        settings: { keep_warm: { ...preferences, hostOfflineArchiveSeconds: 3600 } },
      },
      vi.fn(),
      "alice",
    );

    expect(JSON.parse(localStorage.getItem(KEEP_WARM_STORAGE_KEY) ?? "null")).toEqual({
      ...preferences,
      hostOfflineArchiveSeconds: 3600,
    });
  });

  it("announces an acknowledged patch with its namespace", async () => {
    vi.useFakeTimers();
    const fetcher = vi.fn().mockResolvedValue(new Response(null, { status: 200 }));
    await initializeUserPreferencesSync({ version: 1, settings: {} }, fetcher, "alice");
    const listener = vi.fn();
    window.addEventListener(USER_PREFERENCES_PATCH_ACKNOWLEDGED_EVENT, listener);

    writeKeepWarmPreferences(
      {
        agents: {
          "claude-native-ui": {
            main: true,
            child: false,
            intervalSeconds: 3300,
            maxSeconds: 14400,
            coldAfterSeconds: 0,
          },
        },
        hostOfflineArchiveSeconds: 14400,
      },
      [{ id: "claude-native-ui", harness: "claude-native" }],
    );
    await vi.advanceTimersByTimeAsync(500);

    expect(listener).toHaveBeenCalledWith(
      expect.objectContaining({ detail: { namespace: "keep_warm" } }),
    );
    window.removeEventListener(USER_PREFERENCES_PATCH_ACKNOWLEDGED_EVENT, listener);
  });

  it("cancels queued sync when switching to an older Server", async () => {
    vi.useFakeTimers();
    const currentServer = vi.fn().mockResolvedValue(new Response(null, { status: 200 }));
    await initializeUserPreferencesSync({ version: 1, settings: {} }, currentServer, "alice");
    queueUserPreferencePatch("context_indicator", "compact");

    const oldServer = vi.fn();
    await initializeUserPreferencesSync(undefined, oldServer, "alice");
    await vi.advanceTimersByTimeAsync(500);
    queueUserPreferencePatch("context_indicator", "compact");
    await vi.advanceTimersByTimeAsync(500);

    expect(currentServer).not.toHaveBeenCalled();
    expect(oldServer).not.toHaveBeenCalled();
  });

  it("uploads existing local preferences exactly when the user has no server value", async () => {
    localStorage.setItem("omnigent:context-indicator-mode", "compact");
    const fetcher = vi
      .fn()
      .mockResolvedValue(Response.json({ version: 1, settings: { context_indicator: "compact" } }));
    await initializeUserPreferencesSync(null, fetcher, "alice");
    expect(fetcher).toHaveBeenCalledWith(
      "/v1/me/preferences",
      expect.objectContaining({
        method: "PUT",
        body: JSON.stringify(collectLocalUserPreferences()),
      }),
    );
    expect(localStorage.getItem("omnigent:user-preferences-owner")).toBe("__default__:alice");
  });

  it("does not seed a new account from another user's local cache", async () => {
    localStorage.setItem("omnigent:user-preferences-owner", "__default__:alice");
    localStorage.setItem("omnigent:context-indicator-mode", "compact");
    const fetcher = vi.fn().mockResolvedValue(Response.json({ version: 1, settings: {} }));
    await initializeUserPreferencesSync(null, fetcher, "bob");
    expect(fetcher).toHaveBeenCalledWith(
      "/v1/me/preferences",
      expect.objectContaining({
        body: JSON.stringify({ version: 1, settings: {} }),
      }),
    );
    expect(localStorage.getItem("omnigent:context-indicator-mode")).toBeNull();
    expect(localStorage.getItem("omnigent:user-preferences-owner")).toBe("__default__:bob");
  });

  it("clears another scope's local values even when initialization fails", async () => {
    localStorage.setItem("omnigent:user-preferences-owner", "server-a:alice");
    localStorage.setItem("omnigent:context-indicator-mode", "compact");
    const fetcher = vi.fn().mockResolvedValue(new Response(null, { status: 503 }));

    await initializeUserPreferencesSync(null, fetcher, "alice", "server-b");

    expect(fetcher).toHaveBeenCalledWith(
      "/v1/me/preferences",
      expect.objectContaining({
        body: JSON.stringify({ version: 1, settings: {} }),
      }),
    );
    expect(localStorage.getItem("omnigent:context-indicator-mode")).toBeNull();
    expect(localStorage.getItem("omnigent:user-preferences-owner")).toBe("server-b:alice");
  });

  it("hydrates the persisted winner after a racing first-device initialization", async () => {
    localStorage.setItem("omnigent:context-indicator-mode", "compact");
    const fetcher = vi
      .fn()
      .mockResolvedValue(Response.json({ version: 1, settings: { context_indicator: null } }));
    await initializeUserPreferencesSync(null, fetcher, "alice");
    expect(localStorage.getItem("omnigent:context-indicator-mode")).toBeNull();
  });

  it("lets the server win while retaining device-only assistant position", async () => {
    localStorage.setItem(
      "omnigent:mobile-assistant-preferences",
      JSON.stringify({ version: 2, enabled: true, buttons: [], position: { x: 0.2, y: 0.8 } }),
    );
    const fetcher = vi.fn();
    await initializeUserPreferencesSync(
      {
        version: 1,
        settings: {
          context_indicator: "compact",
          mobile_assistant: { version: 2, enabled: false, buttons: [{ id: "one" }] },
        },
      },
      fetcher,
    );
    expect(localStorage.getItem("omnigent:context-indicator-mode")).toBe("compact");
    expect(JSON.parse(localStorage.getItem("omnigent:mobile-assistant-preferences")!)).toEqual({
      version: 2,
      enabled: false,
      buttons: [{ id: "one" }],
      position: { x: 0.2, y: 0.8 },
    });
    expect(JSON.parse(localStorage.getItem("omnigent:mobile-assistant-device-state")!)).toEqual({
      position: { x: 0.2, y: 0.8 },
    });
  });

  it("retains device-only assistant placement when the user has no assistant namespace", async () => {
    localStorage.setItem(
      "omnigent:mobile-assistant-preferences",
      JSON.stringify({
        version: 2,
        enabled: true,
        buttons: [],
        dock: { edge: "right", offset: 0.4 },
      }),
    );
    await initializeUserPreferencesSync({ version: 1, settings: {} }, vi.fn(), "alice");
    expect(localStorage.getItem("omnigent:mobile-assistant-preferences")).toBeNull();
    expect(JSON.parse(localStorage.getItem("omnigent:mobile-assistant-device-state")!)).toEqual({
      dock: { edge: "right", offset: 0.4 },
    });
  });

  it("ignores device-only assistant placement received from the Server", async () => {
    await initializeUserPreferencesSync(
      {
        version: 1,
        settings: {
          mobile_assistant: {
            version: 2,
            enabled: true,
            buttons: [],
            dock: { edge: "left", offset: 0.25 },
            position: { x: 0.1, y: 0.2 },
          },
        },
      },
      vi.fn(),
    );

    expect(JSON.parse(localStorage.getItem("omnigent:mobile-assistant-preferences")!)).toEqual({
      version: 2,
      enabled: true,
      buttons: [],
    });
    expect(localStorage.getItem("omnigent:mobile-assistant-device-state")).toBeNull();
  });

  it("keeps the latest local assistant placement across later hydration", async () => {
    localStorage.setItem(
      "omnigent:mobile-assistant-preferences",
      JSON.stringify({ version: 2, enabled: true, buttons: [], position: { x: 0.2, y: 0.8 } }),
    );
    const serverEnvelope = {
      version: 1 as const,
      settings: { mobile_assistant: { version: 2, enabled: false, buttons: [] } },
    };
    await initializeUserPreferencesSync(serverEnvelope, vi.fn(), "alice");
    localStorage.setItem(
      "omnigent:mobile-assistant-preferences",
      JSON.stringify({ version: 2, enabled: false, buttons: [], position: { x: 0.6, y: 0.4 } }),
    );

    await initializeUserPreferencesSync(serverEnvelope, vi.fn(), "alice");

    expect(JSON.parse(localStorage.getItem("omnigent:mobile-assistant-preferences")!)).toEqual({
      version: 2,
      enabled: false,
      buttons: [],
      position: { x: 0.6, y: 0.4 },
    });
    expect(JSON.parse(localStorage.getItem("omnigent:mobile-assistant-device-state")!)).toEqual({
      position: { x: 0.6, y: 0.4 },
    });
  });

  it("debounces independent namespace patches", async () => {
    vi.useFakeTimers();
    const fetcher = vi.fn().mockResolvedValue(new Response(null, { status: 200 }));
    await initializeUserPreferencesSync({ version: 1, settings: {} }, fetcher);
    queueUserPreferencePatch("usage_context", { version: 1, showCodexRateLimits: false });
    queueUserPreferencePatch("usage_context", { version: 1, showCodexRateLimits: true });
    await vi.advanceTimersByTimeAsync(251);
    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(fetcher).toHaveBeenCalledWith(
      "/v1/me/preferences/usage_context",
      expect.objectContaining({
        method: "PATCH",
        body: JSON.stringify({ value: { version: 1, showCodexRateLimits: true } }),
      }),
    );
  });

  it("coalesces host colour picks into one full-map patch with an auto reset", async () => {
    vi.useFakeTimers();
    const fetcher = vi.fn().mockResolvedValue(new Response(null, { status: 200 }));
    await initializeUserPreferencesSync({ version: 1, settings: {} }, fetcher, "alice");

    patchHostColor("h1", "blue");
    patchHostColor("h2", "green");
    await vi.advanceTimersByTimeAsync(251);

    const hostColorPatches = fetcher.mock.calls.filter(
      ([url]) => url === "/v1/me/preferences/host_colors",
    );
    expect(hostColorPatches).toHaveLength(1);
    expect(fetcher).toHaveBeenLastCalledWith(
      "/v1/me/preferences/host_colors",
      expect.objectContaining({
        method: "PATCH",
        body: JSON.stringify({ value: { h1: "blue", h2: "green" } }),
      }),
    );

    // A pick then a reset of the same host keeps every key in the map, with
    // the reset as the "auto" tombstone (a shallow merge cannot delete).
    patchHostColor("h3", "red");
    patchHostColor("h3", null);
    await vi.advanceTimersByTimeAsync(251);

    expect(fetcher.mock.calls.at(-1)?.[1]).toEqual(
      expect.objectContaining({
        body: JSON.stringify({ value: { h1: "blue", h2: "green", h3: "auto" } }),
      }),
    );
  });

  it("never replays an acknowledged host colour after hydration or an account switch", async () => {
    vi.useFakeTimers();
    let server: Record<string, string> = {};
    const fetcher = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method === "PATCH") {
        server = { ...server, ...(JSON.parse(String(init.body)).value as Record<string, string>) };
        return new Response(null, { status: 200 });
      }
      return Response.json({
        user_id: "alice",
        preferences: { version: 1, settings: { host_colors: { ...server } } },
      });
    });
    await initializeUserPreferencesSync({ version: 1, settings: {} }, fetcher, "alice");

    patchHostColor("h1", "blue");
    patchHostColor("h2", "green");
    await vi.advanceTimersByTimeAsync(251);
    expect(fetcher.mock.calls.at(-1)?.[1]).toEqual(
      expect.objectContaining({
        body: JSON.stringify({ value: { h1: "blue", h2: "green" } }),
      }),
    );

    // Another client changes h1; the next refresh hydrates the new value.
    server = { ...server, h1: "red" };
    await refreshUserPreferencesFromServer();
    expect(readHostColorPreferences()).toEqual({ h1: "red", h2: "green" });

    patchHostColor("h2", "purple");
    await vi.advanceTimersByTimeAsync(251);
    expect(fetcher.mock.calls.at(-1)?.[1]).toEqual(
      expect.objectContaining({
        body: JSON.stringify({ value: { h1: "red", h2: "purple" } }),
      }),
    );

    // A different account's scope has no colours; the previous account's
    // keys must not ride along with the next edit.
    await initializeUserPreferencesSync({ version: 1, settings: {} }, fetcher, "bob");
    patchHostColor("h3", "orange");
    await vi.advanceTimersByTimeAsync(251);
    expect(fetcher.mock.calls.at(-1)?.[1]).toEqual(
      expect.objectContaining({
        body: JSON.stringify({ value: { h3: "orange" } }),
      }),
    );
  });

  it("serializes overlapping patches so the latest value is persisted last", async () => {
    vi.useFakeTimers();
    let resolveFirst!: (response: Response) => void;
    let resolveSecond!: (response: Response) => void;
    const fetcher = vi
      .fn()
      .mockReturnValueOnce(
        new Promise<Response>((resolve) => {
          resolveFirst = resolve;
        }),
      )
      .mockReturnValueOnce(
        new Promise<Response>((resolve) => {
          resolveSecond = resolve;
        }),
      );
    await initializeUserPreferencesSync({ version: 1, settings: {} }, fetcher, "alice");
    localStorage.setItem("omnigent:context-indicator-mode", "compact");
    queueUserPreferencePatch("context_indicator", "compact");
    await vi.advanceTimersByTimeAsync(251);

    localStorage.removeItem("omnigent:context-indicator-mode");
    queueUserPreferencePatch("context_indicator", null);
    await vi.advanceTimersByTimeAsync(251);

    expect(fetcher).toHaveBeenCalledTimes(1);
    resolveFirst(new Response(null, { status: 200 }));
    await vi.runOnlyPendingTimersAsync();
    expect(fetcher).toHaveBeenCalledTimes(2);
    expect(fetcher.mock.calls.map((call) => (call[1] as RequestInit).body)).toEqual([
      JSON.stringify({ value: "compact" }),
      JSON.stringify({ value: null }),
    ]);
    expect(localStorage.getItem("omnigent:user-preferences-dirty")).not.toBeNull();

    resolveSecond(new Response(null, { status: 200 }));
    await Promise.resolve();
    expect(localStorage.getItem("omnigent:user-preferences-dirty")).toBeNull();
  });

  it("retries an unacknowledged patch and clears its durable dirty marker", async () => {
    vi.useFakeTimers();
    const fetcher = vi
      .fn()
      .mockResolvedValueOnce(new Response(null, { status: 503 }))
      .mockResolvedValueOnce(new Response(null, { status: 200 }));
    await initializeUserPreferencesSync({ version: 1, settings: {} }, fetcher, "alice");
    localStorage.setItem(
      "omnigent:usage-context-preferences",
      JSON.stringify({ version: 1, showCodexRateLimits: true }),
    );
    queueUserPreferencePatch("usage_context", {
      version: 1,
      showCodexRateLimits: true,
    });
    await vi.advanceTimersByTimeAsync(251);
    await vi.runOnlyPendingTimersAsync();
    expect(fetcher).toHaveBeenCalledTimes(2);
    expect(localStorage.getItem("omnigent:user-preferences-dirty")).toBeNull();
  });

  it("cancels a pending account patch when the resolved user changes", async () => {
    vi.useFakeTimers();
    const fetcher = vi.fn().mockResolvedValue(new Response(null, { status: 200 }));
    await initializeUserPreferencesSync({ version: 1, settings: {} }, fetcher, "alice");
    queueUserPreferencePatch("context_indicator", "compact");
    await initializeUserPreferencesSync({ version: 1, settings: {} }, fetcher, "bob");
    await vi.advanceTimersByTimeAsync(500);
    expect(fetcher).not.toHaveBeenCalled();
  });

  it("cancels pending writes when the same owner switches servers", async () => {
    vi.useFakeTimers();
    const serverA = vi.fn().mockResolvedValue(new Response(null, { status: 200 }));
    const serverB = vi.fn().mockResolvedValue(new Response(null, { status: 200 }));
    await initializeUserPreferencesSync({ version: 1, settings: {} }, serverA, "alice", "server-a");
    queueUserPreferencePatch("context_indicator", "compact");

    await initializeUserPreferencesSync({ version: 1, settings: {} }, serverB, "alice", "server-b");
    await vi.advanceTimersByTimeAsync(500);

    expect(serverA).not.toHaveBeenCalled();
    expect(serverB).not.toHaveBeenCalled();
  });

  it("retains an offline edit across A to B to A using the stable Server id", async () => {
    vi.useFakeTimers();
    const serverAFirst = vi.fn().mockResolvedValue(new Response(null, { status: 503 }));
    const serverB = vi.fn();
    const serverAReturn = vi.fn().mockResolvedValue(new Response(null, { status: 200 }));
    await initializeUserPreferencesSync(
      { version: 1, settings: {} },
      serverAFirst,
      "alice",
      "server-a",
      "server-a:connection-1",
    );
    localStorage.setItem("omnigent:context-indicator-mode", "compact");
    queueUserPreferencePatch("context_indicator", "compact");
    await vi.advanceTimersByTimeAsync(251);
    expect(serverAFirst).toHaveBeenCalledTimes(1);

    await initializeUserPreferencesSync(
      { version: 1, settings: {} },
      serverB,
      "alice",
      "server-b",
      "server-b:connection-1",
    );
    expect(localStorage.getItem("omnigent:context-indicator-mode")).toBeNull();
    await initializeUserPreferencesSync(
      { version: 1, settings: {} },
      serverAReturn,
      "alice",
      "server-a",
      "server-a:connection-2",
    );
    expect(localStorage.getItem("omnigent:context-indicator-mode")).toBe("compact");
    await vi.advanceTimersByTimeAsync(251);

    expect(serverB).not.toHaveBeenCalled();
    expect(serverAReturn).toHaveBeenCalledWith(
      "/v1/me/preferences/context_indicator",
      expect.objectContaining({ body: JSON.stringify({ value: "compact" }) }),
    );
    expect(localStorage.getItem("omnigent:user-preferences-dirty")).toBeNull();
  });

  it("recovers an offline edit after a reload on the same stable Server", async () => {
    vi.useFakeTimers();
    const beforeReload = vi.fn().mockResolvedValue(new Response(null, { status: 503 }));
    await initializeUserPreferencesSync(
      { version: 1, settings: {} },
      beforeReload,
      "alice",
      "server-a",
      "server-a:connection-1",
    );
    localStorage.setItem("omnigent:context-indicator-mode", "compact");
    queueUserPreferencePatch("context_indicator", "compact");
    await vi.advanceTimersByTimeAsync(251);

    resetUserPreferencesSyncForTests();
    const afterReload = vi.fn().mockResolvedValue(new Response(null, { status: 200 }));
    await initializeUserPreferencesSync(
      { version: 1, settings: {} },
      afterReload,
      "alice",
      "server-a",
      "server-a:connection-2",
    );
    await vi.advanceTimersByTimeAsync(251);

    expect(afterReload).toHaveBeenCalledWith(
      "/v1/me/preferences/context_indicator",
      expect.objectContaining({ body: JSON.stringify({ value: "compact" }) }),
    );
    expect(localStorage.getItem("omnigent:context-indicator-mode")).toBe("compact");
    expect(localStorage.getItem("omnigent:user-preferences-dirty-values")).toBeNull();
  });

  it("ignores a delayed foreground refresh from the previous server", async () => {
    let resolveA!: (response: Response) => void;
    const serverA = vi.fn().mockReturnValue(
      new Promise<Response>((resolve) => {
        resolveA = resolve;
      }),
    );
    const serverB = vi.fn();
    await initializeUserPreferencesSync(
      { version: 1, settings: { context_indicator: "compact" } },
      serverA,
      "alice",
      "server-a",
    );
    const refresh = refreshUserPreferencesFromServer();
    await initializeUserPreferencesSync(
      { version: 1, settings: { context_indicator: "compact" } },
      serverB,
      "alice",
      "server-b",
    );

    resolveA(
      Response.json({
        user_id: "alice",
        preferences: { version: 1, settings: { context_indicator: null } },
      }),
    );
    await refresh;

    expect(localStorage.getItem("omnigent:context-indicator-mode")).toBe("compact");
  });

  it("refreshes server settings when a long-lived client returns to the foreground", async () => {
    const fetcher = vi.fn().mockResolvedValue(
      Response.json({
        user_id: "alice",
        preferences: { version: 1, settings: {} },
      }),
    );
    await initializeUserPreferencesSync(
      { version: 1, settings: { context_indicator: "compact" } },
      fetcher,
      "alice",
    );
    expect(localStorage.getItem("omnigent:context-indicator-mode")).toBe("compact");

    await refreshUserPreferencesFromServer();

    expect(fetcher).toHaveBeenCalledWith("/v1/me", { cache: "no-store" });
    expect(localStorage.getItem("omnigent:context-indicator-mode")).toBeNull();
  });
});
