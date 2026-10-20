import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  CHILD_IDLE_DEFAULTS,
  CHILD_IDLE_STORAGE_KEY,
  readChildIdlePreferences,
} from "@/lib/childIdlePreferences";
import { ChildIdleSettings } from "./ChildIdleSettings";

const { queuePatch } = vi.hoisted(() => ({ queuePatch: vi.fn() }));
vi.mock("@/lib/userPreferencesSync", () => ({ queueUserPreferencePatch: queuePatch }));
vi.mock("@/hooks/useAvailableAgents", () => ({
  useAvailableAgents: () => ({
    data: [
      { id: "agent-a", display_name: "Agent A" },
      { id: "agent-b", display_name: "Agent B" },
    ],
    isLoading: false,
  }),
}));

beforeEach(() => {
  localStorage.clear();
  queuePatch.mockReset();
});
afterEach(cleanup);

describe("idle child reminders", () => {
  it("starts off and updates its own namespace without changing keep-warm", () => {
    localStorage.setItem("omnigent:keep-warm", '{"agents":{}}');
    render(<ChildIdleSettings />);
    expect(screen.getByRole("spinbutton", { name: "Idle threshold in minutes" })).toBeDisabled();
    fireEvent.click(screen.getByRole("switch", { name: /Remind parent sessions/ }));
    expect(queuePatch).toHaveBeenLastCalledWith("child_idle", {
      ...CHILD_IDLE_DEFAULTS,
      enabled: true,
    });
    for (const [name, value] of [
      ["Idle threshold in minutes", "120"],
      ["Merge window in minutes", "10"],
      ["Repeat interval in minutes", "0"],
    ]) {
      fireEvent.change(screen.getByRole("spinbutton", { name }), { target: { value } });
    }
    expect(readChildIdlePreferences()).toEqual({
      enabled: true,
      idleThresholdSeconds: 7200,
      mergeWindowSeconds: 600,
      repeatSeconds: 0,
      agents: {},
    });
    expect(localStorage.getItem("omnigent:keep-warm")).toBe('{"agents":{}}');
    fireEvent.click(screen.getByRole("switch", { name: /Remind parent sessions/ }));
    expect(readChildIdlePreferences().enabled).toBe(false);
    expect(readChildIdlePreferences().idleThresholdSeconds).toBe(7200);
    expect(screen.getByRole("spinbutton", { name: "Idle threshold in minutes" })).toBeDisabled();
  });

  it("hydrates server changes and recovers corrupt local preferences", () => {
    localStorage.setItem(CHILD_IDLE_STORAGE_KEY, "broken JSON");
    expect(readChildIdlePreferences()).toEqual(CHILD_IDLE_DEFAULTS);
    render(<ChildIdleSettings />);
    localStorage.setItem(
      CHILD_IDLE_STORAGE_KEY,
      JSON.stringify({ enabled: true, idleThresholdSeconds: 1800, mergeWindowSeconds: -1 }),
    );
    fireEvent(window, new Event("omnigent:child-idle-changed"));
    expect(screen.getByRole("spinbutton", { name: "Idle threshold in minutes" })).toHaveValue(30);
    expect(screen.getByRole("spinbutton", { name: "Merge window in minutes" })).toHaveValue(30);
  });

  it("keeps the prior valid number when an out-of-range draft is blurred", () => {
    localStorage.setItem(CHILD_IDLE_STORAGE_KEY, JSON.stringify({ enabled: true }));
    render(<ChildIdleSettings />);
    const input = screen.getByRole("spinbutton", { name: "Idle threshold in minutes" });
    fireEvent.change(input, { target: { value: "0" } });
    fireEvent.blur(input);
    expect(input).toHaveValue(60);
    expect(queuePatch).not.toHaveBeenCalled();
  });

  it("keeps independent agent rows and enables monitoring without idle reminders", () => {
    render(<ChildIdleSettings />);
    fireEvent.click(screen.getByRole("switch", { name: "Monitor Agent A children" }));
    fireEvent.change(screen.getByRole("spinbutton", { name: "Agent A check every in minutes" }), {
      target: { value: "15" },
    });
    fireEvent.click(screen.getByRole("switch", { name: "Resume Agent A once" }));
    expect(readChildIdlePreferences().enabled).toBe(false);
    expect(readChildIdlePreferences().agents["agent-a"]).toEqual({
      enabled: true,
      checkIntervalSeconds: 900,
      freezeThresholdSeconds: 3600,
      resumeOnce: true,
    });
    expect(screen.getByRole("switch", { name: "Monitor Agent B children" })).not.toBeChecked();
    expect(screen.getByRole("spinbutton", { name: "Merge window in minutes" })).toBeEnabled();
  });
});
