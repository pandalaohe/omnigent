import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  CHILD_IDLE_DEFAULTS,
  CHILD_IDLE_STORAGE_KEY,
  readChildIdlePreferences,
} from "@/lib/childIdlePreferences";
import { ChildIdleSettings } from "./ChildIdleSettings";

const { queuePatch, registryState } = vi.hoisted(() => ({
  queuePatch: vi.fn(),
  registryState: { isError: false, retry: vi.fn() },
}));
vi.mock("@/lib/userPreferencesSync", () => ({ queueUserPreferencePatch: queuePatch }));
vi.mock("@/hooks/useAgentRegistry", () => ({
  SECTION_LABELS: {
    yours: "Your agents",
    native: "Native CLI",
    sdk: "SDK",
    acp: "ACP",
    builtin: "Built-in agents",
    other: "Other",
  },
  SECTION_ORDER: ["yours", "native", "sdk", "acp", "builtin", "other"],
  SECTION_GLOSSES: {},
  useAgentRegistry: () => ({
    items: [
      {
        key: "agent-a",
        label: "Agent A",
        section: "yours",
        harness: "example",
        description: null,
        deployed: true,
        source: "installed",
      },
      {
        key: "agent-b",
        label: "Agent B",
        section: "yours",
        harness: "example",
        description: null,
        deployed: true,
        source: "installed",
      },
    ],
    legacyLabels: {},
    isLoading: false,
    isError: registryState.isError,
    retry: registryState.retry,
  }),
}));

beforeEach(() => {
  localStorage.clear();
  queuePatch.mockReset();
  registryState.isError = false;
  registryState.retry.mockReset();
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
      freezeDefault: CHILD_IDLE_DEFAULTS.freezeDefault,
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

  it("uses one default and adds only requested exceptions", () => {
    render(<ChildIdleSettings />);
    expect(screen.getAllByRole("switch", { name: /Monitor/ })).toHaveLength(1);
    fireEvent.click(screen.getByRole("button", { name: "Add exception" }));
    fireEvent.click(screen.getByRole("button", { name: /Agent A.*Your agents/ }));
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
    expect(
      screen.queryByRole("switch", { name: "Monitor Agent B children" }),
    ).not.toBeInTheDocument();
    expect(screen.getByRole("spinbutton", { name: "Merge window in minutes" })).toBeEnabled();
  });

  it("keeps a legacy raw-id row while saving a new harness exception", () => {
    localStorage.setItem(
      CHILD_IDLE_STORAGE_KEY,
      JSON.stringify({
        agents: {
          "old-agent": {
            enabled: true,
            checkIntervalSeconds: 600,
            freezeThresholdSeconds: 1200,
            resumeOnce: false,
          },
        },
        freezeDefault: {
          enabled: true,
          checkIntervalSeconds: 1800,
          freezeThresholdSeconds: 2400,
          resumeOnce: true,
        },
      }),
    );
    render(<ChildIdleSettings />);
    expect(screen.getByText("old-agent")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Add exception" }));
    fireEvent.click(screen.getByRole("button", { name: /Agent B.*Your agents/ }));
    expect(readChildIdlePreferences().agents["old-agent"]).toEqual({
      enabled: true,
      checkIntervalSeconds: 600,
      freezeThresholdSeconds: 1200,
      resumeOnce: false,
    });
    expect(readChildIdlePreferences().freezeDefault).toEqual({
      enabled: true,
      checkIntervalSeconds: 1800,
      freezeThresholdSeconds: 2400,
      resumeOnce: true,
    });
    expect(readChildIdlePreferences().agents["agent-b"]).toEqual(
      readChildIdlePreferences().freezeDefault,
    );
  });

  it("keeps defaults editable and offers retry when registry loading fails", () => {
    registryState.isError = true;
    render(<ChildIdleSettings />);
    fireEvent.click(screen.getByRole("switch", { name: "Monitor all agents children" }));
    expect(readChildIdlePreferences().freezeDefault.enabled).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "Add exception" }));
    expect(screen.getByText("Couldn't load agents")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(registryState.retry).toHaveBeenCalledOnce();
  });
});
