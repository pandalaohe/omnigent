import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  AGENT_BADGE_CHANGED_EVENT,
  AGENT_BADGE_STORAGE_KEY,
  DEFAULT_AGENT_BADGE_PREFERENCES,
  agentBadgeFor,
  normalizeAgentBadgePreferences,
  readAgentBadgePreferences,
  validateAgentBadgeLabel,
  writeAgentBadgePreferences,
} from "./agentBadgePreferences";
import { queueUserPreferencePatch } from "./userPreferencesSync";

vi.mock("./userPreferencesSync", () => ({ queueUserPreferencePatch: vi.fn() }));

beforeEach(() => {
  localStorage.clear();
  vi.mocked(queueUserPreferencePatch).mockReset();
});

describe("agent badge preferences", () => {
  it("starts enabled with no forced badge entries", () => {
    expect(readAgentBadgePreferences()).toEqual(DEFAULT_AGENT_BADGE_PREFERENCES);
    expect(localStorage.getItem(AGENT_BADGE_STORAGE_KEY)).toBeNull();
  });

  it("preserves theme-following text through storage and account sync", () => {
    const preferences = {
      version: 1 as const,
      enabled: true,
      entries: {
        "agent-theme": { label: "助", borderColor: "#3b82f6", textColor: "theme" },
      },
    };
    writeAgentBadgePreferences(preferences);
    expect(readAgentBadgePreferences()).toEqual(preferences);
    expect(queueUserPreferencePatch).toHaveBeenLastCalledWith("agent_badges", preferences);
  });

  it.each([
    ["助", null],
    ["A", null],
    ["AI", null],
    ["🧭", null],
    ["助手", "Use one wide symbol or up to two narrow characters."],
    ["ＡＢ", "Use one wide symbol or up to two narrow characters."],
    ["ABC", "Use one wide symbol or up to two narrow characters."],
    ["A B", "Badge text cannot contain spaces or control characters."],
  ])("validates the compact label %j", (label, expected) => {
    expect(validateAgentBadgeLabel(label)).toBe(expected);
  });

  it("drops malformed rows while retaining valid Agent-id keyed rows", () => {
    expect(
      normalizeAgentBadgePreferences({
        version: 1,
        enabled: false,
        entries: {
          "agent-a": { label: "AI", borderColor: "#AABBCC", textColor: "#123456" },
          "agent-b": { label: "助手", borderColor: "#aabbcc", textColor: "#123456" },
          "agent-c": { label: "C", borderColor: "red", textColor: "#123456" },
        },
      }),
    ).toEqual({
      version: 1,
      enabled: false,
      entries: {
        "agent-a": { label: "AI", borderColor: "#aabbcc", textColor: "#123456" },
      },
    });
  });

  it("persists a disabled global switch without deleting individual badges", () => {
    const changed = vi.fn();
    window.addEventListener(AGENT_BADGE_CHANGED_EVENT, changed);
    const preferences = {
      version: 1 as const,
      enabled: false,
      entries: {
        "agent-a": { label: "A", borderColor: "#123456", textColor: "#abcdef" },
      },
    };

    writeAgentBadgePreferences(preferences);

    expect(readAgentBadgePreferences()).toEqual(preferences);
    expect(changed).toHaveBeenCalledOnce();
    expect(queueUserPreferencePatch).toHaveBeenCalledWith("agent_badges", preferences);
    window.removeEventListener(AGENT_BADGE_CHANGED_EVENT, changed);
  });

  it("removes the default local value and syncs a null namespace", () => {
    localStorage.setItem(AGENT_BADGE_STORAGE_KEY, "stale");
    writeAgentBadgePreferences(DEFAULT_AGENT_BADGE_PREFERENCES);
    expect(localStorage.getItem(AGENT_BADGE_STORAGE_KEY)).toBeNull();
    expect(queueUserPreferencePatch).toHaveBeenCalledWith("agent_badges", null);
  });

  it("does not resolve inherited object keys as Agent ids", () => {
    expect(DEFAULT_AGENT_BADGE_PREFERENCES.entries.toString).toBeDefined();
    expect(agentBadgeFor(DEFAULT_AGENT_BADGE_PREFERENCES, "toString")).toBeNull();
  });
});
