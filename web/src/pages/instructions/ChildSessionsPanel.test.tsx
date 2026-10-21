import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

const { queuePatchMock } = vi.hoisted(() => ({ queuePatchMock: vi.fn() }));
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
import {
  readSessionCollabPreferences,
  SESSION_COLLAB_DEFAULTS,
  writeSessionCollabPreferences,
} from "@/lib/sessionCollabPreferences";

afterEach(() => {
  cleanup();
  localStorage.clear();
  queuePatchMock.mockReset();
});

describe("ChildSessionsPanel", () => {
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
