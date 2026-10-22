import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { KEEP_WARM_STORAGE_KEY, readKeepWarmPreferences } from "@/lib/keepWarmPreferences";
import { SESSION_COLLAB_STORAGE_KEY } from "@/lib/sessionCollabPreferences";
import { KeepWarmSettings } from "./KeepWarmSettings";

const { queuePatch, registryState } = vi.hoisted(() => ({
  queuePatch: vi.fn(),
  registryState: { isError: false, retry: vi.fn() },
}));
vi.mock("@/lib/userPreferencesSync", () => ({ queueUserPreferencePatch: queuePatch }));
vi.mock("@/lib/agentLabels", () => ({
  useHarnessCatalog: (select: (catalog: unknown) => unknown) =>
    select({
      capabilitiesByHarness: {
        "claude-native": { keep_warm_family: "claude" },
        "codex-native": { keep_warm_family: "codex" },
      },
    }),
}));
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
        key: "harness:claude-native",
        label: "Claude Code",
        section: "native",
        harness: "claude-native",
        description: null,
        deployed: true,
        source: "builtin",
      },
      {
        key: "ca_codex",
        label: "My Codex",
        section: "yours",
        harness: "codex-native",
        description: null,
        deployed: true,
        source: "installed",
      },
      {
        key: "other-agent",
        label: "Other",
        section: "yours",
        harness: "other",
        description: null,
        deployed: true,
        source: "installed",
      },
      {
        key: "offline-agent",
        label: "Offline",
        section: "yours",
        harness: "claude-native",
        description: null,
        deployed: false,
        source: "installed",
      },
    ],
    legacyLabels: { "old-product": "Old Claude Code" },
    legacyProducts: {
      "old-product": {
        key: "old-product",
        label: "Old Claude Code",
        section: "native",
        harness: "claude-native",
        description: null,
        deployed: true,
        source: "builtin",
      },
    },
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

describe("KeepWarmSettings", () => {
  it("shows one editable default, with no family interval or per-agent overview", () => {
    render(<KeepWarmSettings />);
    expect(screen.getAllByRole("switch")).toHaveLength(2);
    expect(screen.queryByText("Agent overview")).not.toBeInTheDocument();
    expect(
      screen.queryByRole("spinbutton", { name: /Keep-warm interval/ }),
    ).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("switch", { name: "Keep main sessions warm for all agents" }));
    fireEvent.click(screen.getByRole("switch", { name: "Keep children warm for all agents" }));
    fireEvent.change(
      screen.getByRole("spinbutton", { name: "Longest keep-warm run for all agents in hours" }),
      { target: { value: "6" } },
    );
    expect(readKeepWarmPreferences().default).toEqual({
      main: true,
      child: true,
      maxSeconds: 21600,
    });
    expect(readKeepWarmPreferences().agents).toEqual({});
    expect(queuePatch).toHaveBeenLastCalledWith("keep_warm", readKeepWarmPreferences());
  });

  it("adds an exception from the shared picker and keeps the family interval bounds", () => {
    render(<KeepWarmSettings />);
    fireEvent.click(screen.getByRole("button", { name: "Add exception" }));
    fireEvent.click(screen.getByRole("button", { name: /Claude Code.*Native CLI/ }));
    fireEvent.click(
      screen.getByRole("switch", { name: "Keep main sessions warm for Claude Code" }),
    );
    fireEvent.change(
      screen.getByRole("spinbutton", { name: "Keep-warm interval for Claude Code in minutes" }),
      { target: { value: "55" } },
    );
    expect(readKeepWarmPreferences().agents["harness:claude-native"]).toEqual({
      main: true,
      child: false,
      intervalSeconds: 3300,
      maxSeconds: 14400,
    });
    expect(screen.getByRole("button", { name: "Edit Claude Code" })).toHaveTextContent("Done");
    expect(
      screen.queryByRole("switch", { name: "Keep main sessions warm for My Codex" }),
    ).not.toBeInTheDocument();
  });

  it("keeps saved direct-agent and unsupported rows while writing another exception", () => {
    localStorage.setItem(
      KEEP_WARM_STORAGE_KEY,
      JSON.stringify({
        agents: {
          "old-product": { main: true, child: false, intervalSeconds: 3300, maxSeconds: 7200 },
          "missing-agent": { main: false, child: true, intervalSeconds: 600, maxSeconds: 14400 },
        },
        hostOfflineArchiveSeconds: 0,
      }),
    );
    render(<KeepWarmSettings />);
    expect(screen.getByText(/Old Claude Code · direct sessions only/)).toBeInTheDocument();
    expect(screen.getByText("missing-agent")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Add exception" }));
    fireEvent.click(screen.getByRole("button", { name: /My Codex.*Your agents/ }));
    expect(readKeepWarmPreferences().agents["old-product"].intervalSeconds).toBe(3300);
    expect(readKeepWarmPreferences().agents["missing-agent"].child).toBe(true);
    expect(readKeepWarmPreferences().agents.ca_codex.intervalSeconds).toBe(1500);
    expect(readKeepWarmPreferences().hostOfflineArchiveSeconds).toBe(0);
    fireEvent.click(
      screen.getByRole("button", { name: "Edit Old Claude Code · direct sessions only" }),
    );
    expect(
      screen.getByRole("spinbutton", { name: "Keep-warm interval for Old Claude Code in minutes" }),
    ).toHaveValue(55);
    fireEvent.change(
      screen.getByRole("spinbutton", { name: "Keep-warm interval for Old Claude Code in minutes" }),
      { target: { value: "20" } },
    );
    fireEvent.click(screen.getByRole("switch", { name: "Keep children warm for Old Claude Code" }));
    expect(readKeepWarmPreferences().agents["old-product"].intervalSeconds).toBe(1200);
    expect(
      JSON.parse(localStorage.getItem(SESSION_COLLAB_STORAGE_KEY) ?? "{}").childKeepWarmEnabled,
    ).toBe(true);
  });

  it("copies default cold-after into a new exception, then clears it to the platform rule", () => {
    render(<KeepWarmSettings />);
    fireEvent.change(
      screen.getByRole("spinbutton", { name: "Cold after for all agents in minutes" }),
      { target: { value: "0" } },
    );
    fireEvent.click(screen.getByRole("button", { name: "Add exception" }));
    fireEvent.click(screen.getByRole("button", { name: /Claude Code.*Native CLI/ }));
    expect(readKeepWarmPreferences().agents["harness:claude-native"]).toEqual({
      main: false,
      child: false,
      intervalSeconds: 3300,
      maxSeconds: 14400,
      coldAfterSeconds: 0,
    });
    const cold = screen.getByRole("spinbutton", { name: "Cold after for Claude Code in minutes" });
    expect(cold).toHaveValue(0);
    fireEvent.change(cold, { target: { value: "" } });
    expect(
      readKeepWarmPreferences().agents["harness:claude-native"].coldAfterSeconds,
    ).toBeUndefined();
    expect(cold).toHaveValue(null);
  });

  it("shows unsupported picker items with a reason and retains the host control", () => {
    render(<KeepWarmSettings />);
    fireEvent.click(screen.getByRole("button", { name: "Add exception" }));
    fireEvent.click(screen.getByRole("button", { name: "Not supported here (1)" }));
    expect(
      screen.getByRole("button", { name: /Other.*This agent has no declared keep-warm channel/ }),
    ).toBeDisabled();
    fireEvent.change(
      screen.getByRole("spinbutton", {
        name: "Archive children of an offline host after in hours",
      }),
      { target: { value: "0" } },
    );
    expect(readKeepWarmPreferences().hostOfflineArchiveSeconds).toBe(0);
  });

  it("keeps defaults editable and offers retry on a registry error", () => {
    registryState.isError = true;
    render(<KeepWarmSettings />);
    fireEvent.click(screen.getByRole("switch", { name: "Keep main sessions warm for all agents" }));
    expect(readKeepWarmPreferences().default.main).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "Add exception" }));
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(registryState.retry).toHaveBeenCalledOnce();
  });
});

it("shows the effective family interval for a saved row with no interval", () => {
  localStorage.setItem(
    KEEP_WARM_STORAGE_KEY,
    JSON.stringify({ agents: { "old-product": { main: true, child: false, maxSeconds: 14400 } } }),
  );
  render(<KeepWarmSettings />);
  fireEvent.click(screen.getByRole("button", { name: /Edit Old Claude Code/ }));
  expect(screen.getByRole("spinbutton", { name: /Keep-warm interval/ })).toHaveValue(55);
});
