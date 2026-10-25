import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const { queuePatchMock } = vi.hoisted(() => ({ queuePatchMock: vi.fn() }));
const { fetchMock } = vi.hoisted(() => ({ fetchMock: vi.fn() }));
vi.mock("@/lib/identity", () => ({
  authenticatedFetch: fetchMock,
  getCurrentUserId: () => "alice",
  subscribeIdentity: () => () => {},
}));
vi.mock("@/lib/userPreferencesSync", () => ({
  queueUserPreferencePatch: queuePatchMock,
  USER_PREFERENCES_PATCH_REJECTED_EVENT: "omnigent:user-preference-rejected",
}));
vi.mock("@/lib/agentLabels", () => ({
  useHarnessCatalog: (select: (catalog: unknown) => unknown) =>
    select({
      capabilitiesByHarness: {
        "claude-native": { native_agent_key: "claude" },
        "codex-native": { native_agent_key: "codex" },
        "kite-native": { native_agent_key: "kite" },
        polly: { native_agent_key: null },
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
        key: "harness:codex-native",
        label: "Codex",
        section: "native",
        harness: "codex-native",
        description: null,
        deployed: true,
        source: "builtin",
      },
      {
        key: "harness:kite-native",
        label: "Kite",
        section: "native",
        harness: "kite-native",
        description: null,
        deployed: true,
        source: "builtin",
      },
      {
        key: "harness:polly",
        label: "Polly",
        section: "builtin",
        harness: "polly",
        description: null,
        deployed: true,
        source: "builtin",
      },
    ],
    isLoading: false,
    isError: false,
    retry: vi.fn(),
  }),
}));

import { ChildSessionsPanel } from "./ChildSessionsPanel";
import { readChildIdlePreferences, writeChildIdlePreferences } from "@/lib/childIdlePreferences";
import {
  readSessionCollabPreferences,
  SESSION_COLLAB_DEFAULTS,
  writeSessionCollabPreferences,
} from "@/lib/sessionCollabPreferences";

beforeEach(() => {
  fetchMock.mockResolvedValue({
    ok: true,
    json: async () => ({ instructions: "Collect results first." }),
  });
});

afterEach(() => {
  cleanup();
  localStorage.clear();
  queuePatchMock.mockReset();
  fetchMock.mockReset();
});

describe("ChildSessionsPanel", () => {
  it("loads the default and saves custom, opt-out and reset without dropping idle settings", async () => {
    writeChildIdlePreferences({
      ...readChildIdlePreferences(),
      minGapSeconds: 7200,
      enabled: true,
    });
    render(<ChildSessionsPanel />);
    const editor = screen.getByRole("textbox", { name: "Check-in instructions" });
    await waitFor(() => expect(editor).toHaveValue("Collect results first."));
    expect(fetchMock).toHaveBeenCalledWith("/v1/child-checkin/defaults", expect.any(Object));
    expect(screen.getByText("22 / 4000 characters")).toBeInTheDocument();
    fireEvent.change(editor, { target: { value: "Custom 😀" } });
    expect(screen.getByText("8 / 4000 characters")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Save check-in instructions" }));
    expect(readChildIdlePreferences()).toMatchObject({
      checkinInstructions: "Custom 😀",
      minGapSeconds: 7200,
      enabled: true,
    });
    fireEvent.click(screen.getByRole("button", { name: "Send no instructions" }));
    fireEvent.click(screen.getByRole("button", { name: "Save check-in instructions" }));
    expect(readChildIdlePreferences().checkinInstructions).toBe("");
    fireEvent.click(screen.getByRole("button", { name: "Reset to default" }));
    expect(editor).toHaveValue("Collect results first.");
    fireEvent.click(screen.getByRole("button", { name: "Save check-in instructions" }));
    expect(queuePatchMock).toHaveBeenLastCalledWith(
      "child_idle",
      expect.objectContaining({ checkinInstructions: null, minGapSeconds: 7200 }),
    );
    writeChildIdlePreferences({ ...readChildIdlePreferences(), checkinInstructions: "Keep me" });
    writeChildIdlePreferences({ ...readChildIdlePreferences(), enabled: false });
    expect(readChildIdlePreferences().checkinInstructions).toBe("Keep me");
  });

  it("rejects blank-only and overlong drafts and reports an instruction save rejection", async () => {
    render(<ChildSessionsPanel />);
    const editor = screen.getByRole("textbox", { name: "Check-in instructions" });
    await waitFor(() => expect(editor).toBeEnabled());
    for (const value of ["   ", "x".repeat(4001)]) {
      fireEvent.change(editor, { target: { value } });
      fireEvent.click(screen.getByRole("button", { name: "Save check-in instructions" }));
      expect(screen.getByRole("alert")).toBeInTheDocument();
      expect(queuePatchMock).not.toHaveBeenCalled();
    }
    fireEvent.change(editor, { target: { value: "Collect" } });
    fireEvent.click(screen.getByRole("button", { name: "Save check-in instructions" }));
    fireEvent(
      window,
      new CustomEvent("omnigent:user-preference-rejected", { detail: { namespace: "child_idle" } }),
    );
    expect(screen.getByRole("alert")).toHaveTextContent("server rejected these instructions");
  });

  it("retries failed default loading", async () => {
    fetchMock.mockRejectedValueOnce(new Error("offline"));
    render(<ChildSessionsPanel />);
    await screen.findByText(/Could not load default instructions/);
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() =>
      expect(screen.getByRole("textbox", { name: "Check-in instructions" })).toHaveValue(
        "Collect results first.",
      ),
    );
  });

  it("keeps unsaved instructions through preference hydration and unrelated storage events", async () => {
    render(<ChildSessionsPanel />);
    const editor = screen.getByRole("textbox", { name: "Check-in instructions" });
    await waitFor(() => expect(editor).toBeEnabled());
    fireEvent.change(editor, { target: { value: "Unsaved draft" } });
    fireEvent(window, new Event("omnigent:child-idle-changed"));
    expect(editor).toHaveValue("Unsaved draft");
    fireEvent(window, new StorageEvent("storage", { key: "unrelated" }));
    expect(editor).toHaveValue("Unsaved draft");
    writeChildIdlePreferences({ ...readChildIdlePreferences(), enabled: true });
    expect(editor).toHaveValue("Unsaved draft");
    fireEvent.click(screen.getByRole("button", { name: "Save check-in instructions" }));
    expect(readChildIdlePreferences()).toMatchObject({
      checkinInstructions: "Unsaved draft",
      enabled: true,
    });
    fireEvent.change(editor, { target: { value: "Newer unsaved draft" } });
    fireEvent(
      window,
      new CustomEvent("omnigent:user-preference-rejected", {
        detail: { namespace: "child_idle", status: 422 },
      }),
    );
    expect(editor).toHaveValue("Newer unsaved draft");
    expect(screen.getByRole("alert")).toHaveTextContent("server rejected these instructions");
  });

  it("refreshes a clean editor when a rejected save is rolled back by the server", async () => {
    render(<ChildSessionsPanel />);
    const editor = screen.getByRole("textbox", { name: "Check-in instructions" });
    await waitFor(() => expect(editor).toBeEnabled());
    fireEvent.change(editor, { target: { value: "Saved draft" } });
    fireEvent.click(screen.getByRole("button", { name: "Save check-in instructions" }));
    fireEvent(window, new StorageEvent("storage", { key: "unrelated" }));
    expect(editor).toHaveValue("Saved draft");
    fireEvent(
      window,
      new CustomEvent("omnigent:user-preference-rejected", {
        detail: { namespace: "child_idle", status: 422 },
      }),
    );
    expect(editor).toHaveValue("Saved draft");
    fireEvent(window, new Event("omnigent:child-idle-changed"));
    expect(editor).toHaveValue("Saved draft");
    writeChildIdlePreferences({
      ...readChildIdlePreferences(),
      checkinInstructions: "Server value",
    });
    await waitFor(() => expect(editor).toHaveValue("Server value"));
  });

  it("saves child controls without dropping existing collaboration preferences", () => {
    writeSessionCollabPreferences({ ...SESSION_COLLAB_DEFAULTS, relayDepthMax: 3 });
    render(<ChildSessionsPanel />);
    fireEvent.change(screen.getByRole("textbox", { name: /Child session limit/ }), {
      target: { value: "4" },
    });
    fireEvent.change(screen.getByRole("textbox", { name: /Tight below/ }), {
      target: { value: "45.5" },
    });
    fireEvent.change(screen.getByRole("textbox", { name: /Low below/ }), {
      target: { value: "15" },
    });
    fireEvent.change(screen.getByRole("textbox", { name: /Remaining quota floor/ }), {
      target: { value: "0" },
    });
    fireEvent.change(screen.getByRole("textbox", { name: "All products" }), {
      target: { value: "5h" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Add exception" }));
    fireEvent.click(screen.getByRole("button", { name: /Claude Code.*Native CLI/ }));
    fireEvent.change(screen.getByRole("textbox", { name: /Claude Code period/ }), {
      target: { value: "7d" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save child settings" }));

    expect(readSessionCollabPreferences()).toMatchObject({
      relayDepthMax: 3,
      childCap: 4,
      quotaTightBelowPct: 45.5,
      quotaLowBelowPct: 15,
      quotaFloorPct: 0,
      quotaPeriodDefault: "5h",
      quotaPeriods: { claude: "7d" },
    });
    expect(queuePatchMock).toHaveBeenLastCalledWith(
      "session_collab",
      expect.objectContaining({ relayDepthMax: 3, childCap: 4 }),
    );
    expect(screen.getByRole("status")).toHaveTextContent("Saved preferences.");
  });

  it("keeps legacy exceptions and offers a registry plugin without twelve inputs", () => {
    writeSessionCollabPreferences({
      ...SESSION_COLLAB_DEFAULTS,
      quotaPeriods: { codex: "7d" },
    });
    render(<ChildSessionsPanel />);
    expect(screen.getByRole("textbox", { name: "All products" })).toHaveValue("");
    expect(screen.getByText("7d")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Edit Codex" })).toBeInTheDocument();
    expect(screen.queryByRole("textbox", { name: /Claude Code period/ })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Add exception" }));
    fireEvent.click(screen.getByRole("button", { name: /Kite.*Native CLI/ }));
    fireEvent.change(screen.getByRole("textbox", { name: /Kite period/ }), {
      target: { value: "month" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save child settings" }));
    expect(readSessionCollabPreferences()).toMatchObject({
      quotaPeriodDefault: null,
      quotaPeriods: { codex: "7d", kite: "month" },
    });
  });

  it("requires a period before saving an added exception", () => {
    render(<ChildSessionsPanel />);
    fireEvent.click(screen.getByRole("button", { name: "Add exception" }));
    fireEvent.click(screen.getByRole("button", { name: /Kite.*Native CLI/ }));
    fireEvent.click(screen.getByRole("button", { name: "Save child settings" }));
    expect(screen.getByRole("alert")).toHaveTextContent("Enter a period for each exception");
    expect(queuePatchMock).not.toHaveBeenCalled();
  });

  it("keeps invalid thresholds in the draft until corrected", () => {
    render(<ChildSessionsPanel />);
    fireEvent.change(screen.getByRole("textbox", { name: /Low below/ }), {
      target: { value: "60" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save child settings" }));
    expect(screen.getByRole("alert")).toHaveTextContent("0 < low < tight");
    expect(readSessionCollabPreferences()).toEqual(SESSION_COLLAB_DEFAULTS);
    expect(queuePatchMock).not.toHaveBeenCalled();

    fireEvent.change(screen.getByRole("textbox", { name: /Low below/ }), {
      target: { value: "10" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save child settings" }));
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(readSessionCollabPreferences().quotaLowBelowPct).toBe(10);
  });

  it("reports a server rejection after a local save", () => {
    render(<ChildSessionsPanel />);
    fireEvent.change(screen.getByRole("textbox", { name: /Child session limit/ }), {
      target: { value: "2" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save child settings" }));
    fireEvent(
      window,
      new CustomEvent("omnigent:user-preference-rejected", {
        detail: { namespace: "session_collab", status: 422 },
      }),
    );
    expect(screen.getByRole("alert")).toHaveTextContent("server rejected");
  });
});
