import { afterEach, describe, expect, it, vi } from "vitest";

const { queuePatchMock } = vi.hoisted(() => ({ queuePatchMock: vi.fn() }));
vi.mock("./userPreferencesSync", () => ({ queueUserPreferencePatch: queuePatchMock }));

import {
  addCallingDefaultEntry,
  readCallingDefaults,
  readCallingLast,
  readCallingLastEnabled,
  recordCallingLast,
  removeCallingDefaultEntry,
  setCallingDefaultField,
  writeCallingLastEnabled,
} from "./callingDefaults";

const DEFAULTS_KEY = "omnigent:calling-defaults";
const LAST_KEY = "omnigent:calling-last";

afterEach(() => {
  localStorage.clear();
  queuePatchMock.mockReset();
  vi.useRealTimers();
});

describe("calling_defaults master table", () => {
  it("treats a malformed table as empty and drops malformed entries", () => {
    localStorage.setItem(DEFAULTS_KEY, '"nope"');
    expect(readCallingDefaults()).toEqual({});
    localStorage.setItem(
      DEFAULTS_KEY,
      JSON.stringify({ "host-a": { codex: { model: 5 }, "host-b": "bad" } }),
    );
    expect(readCallingDefaults()).toEqual({ "host-a": { codex: {} } });
  });

  it("patches one host key at a time so other hosts survive the shallow merge", () => {
    setCallingDefaultField("host-a", "codex-native", "model", "gpt-6-astra");
    setCallingDefaultField("host-b", "claude-native", "effort", "high");

    expect(queuePatchMock).toHaveBeenNthCalledWith(1, "calling_defaults", {
      "host-a": { "codex-native": { model: "gpt-6-astra" } },
    });
    expect(queuePatchMock).toHaveBeenNthCalledWith(2, "calling_defaults", {
      "host-b": { "claude-native": { effort: "high" } },
    });
    expect(readCallingDefaults()).toEqual({
      "host-a": { "codex-native": { model: "gpt-6-astra" } },
      "host-b": { "claude-native": { effort: "high" } },
    });
  });

  it("clears a field on Default and never stores a clear word", () => {
    setCallingDefaultField("host-a", "codex-native", "model", "gpt-6-astra");
    setCallingDefaultField("host-a", "codex-native", "effort", "high");
    setCallingDefaultField("host-a", "codex-native", "effort", null);
    expect(readCallingDefaults()).toEqual({
      "host-a": { "codex-native": { model: "gpt-6-astra" } },
    });

    setCallingDefaultField("host-a", "codex-native", "effort", "default");
    expect(readCallingDefaults()).toEqual({
      "host-a": { "codex-native": { model: "gpt-6-astra" } },
    });
    const payload = queuePatchMock.mock.calls.at(-1)?.[1];
    expect(JSON.stringify(payload)).not.toContain("default");
  });

  it("preserves speed and permission and clears them independently", () => {
    setCallingDefaultField("host-a", "codex-native", "speed", "fast");
    setCallingDefaultField("host-a", "codex-native", "permission", "approve-for-me");
    expect(readCallingDefaults()).toEqual({
      "host-a": { "codex-native": { speed: "fast", permission: "approve-for-me" } },
    });
    setCallingDefaultField("host-a", "codex-native", "speed", null);
    expect(readCallingDefaults()["host-a"]["codex-native"]).toEqual({
      permission: "approve-for-me",
    });
    setCallingDefaultField("host-a", "codex-native", "permission", null);
    expect(readCallingDefaults()["host-a"]["codex-native"]).toEqual({});
  });

  it("adds and removes a harness row without touching siblings", () => {
    addCallingDefaultEntry("host-a", "codex-native");
    addCallingDefaultEntry("host-a", "claude-native");
    expect(readCallingDefaults()).toEqual({
      "host-a": { "codex-native": {}, "claude-native": {} },
    });

    removeCallingDefaultEntry("host-a", "codex-native");
    expect(readCallingDefaults()).toEqual({ "host-a": { "claude-native": {} } });
    expect(queuePatchMock.mock.calls.at(-1)?.[1]).toEqual({
      "host-a": { "claude-native": {} },
    });
  });
});

describe("calling_last memory", () => {
  it("defaults carry-over to off and patches only enabled on toggle", () => {
    expect(readCallingLastEnabled()).toBe(false);
    writeCallingLastEnabled(true);
    expect(readCallingLastEnabled()).toBe(true);
    expect(queuePatchMock).toHaveBeenLastCalledWith("calling_last", { enabled: true });
    writeCallingLastEnabled(false);
    expect(readCallingLastEnabled()).toBe(false);
  });

  it("records the last agent per project and host without dropping siblings", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-09-28T14:02:00Z"));
    const at = Math.floor(new Date("2026-09-28T14:02:00Z").getTime() / 1000);

    recordCallingLast("p1", "host-a", "ag_codex", { harness: "codex", model: "gpt-6-luna" });
    recordCallingLast("p1", "host-a", "ag_claude", {
      harness: "claude-sdk",
      model: "opus-5-5",
      effort: "xhigh",
    });
    recordCallingLast("p2", "host-a", "ag_codex", { harness: "codex" });

    expect(readCallingLast()).toEqual({
      enabled: false,
      projects: {
        "p:p1": {
          "host-a": {
            last_agent_id: "ag_claude",
            agents: {
              ag_codex: { harness: "codex", model: "gpt-6-luna", at },
              ag_claude: { harness: "claude-sdk", model: "opus-5-5", effort: "xhigh", at },
            },
          },
        },
        "p:p2": {
          "host-a": {
            last_agent_id: "ag_codex",
            agents: { ag_codex: { harness: "codex", at } },
          },
        },
      },
    });
    // The last patch carries only the project key it touched, with every host
    // read from the local cache.
    expect(queuePatchMock).toHaveBeenLastCalledWith("calling_last", {
      "p:p2": {
        "host-a": {
          last_agent_id: "ag_codex",
          agents: { ag_codex: { harness: "codex", at } },
        },
      },
    });
  });

  it("ignores a record with no harness", () => {
    recordCallingLast("p1", "host-a", "ag_codex", { harness: "  " });
    expect(localStorage.getItem(LAST_KEY)).toBeNull();
    expect(queuePatchMock).not.toHaveBeenCalled();
  });
});
