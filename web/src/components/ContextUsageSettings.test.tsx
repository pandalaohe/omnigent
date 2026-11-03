import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { Host } from "@/hooks/useHosts";
import type { RegistryItem } from "@/hooks/useAgentRegistry";
import type { ProviderUsageLimitsSnapshot } from "@/lib/providerUsageLimits";
import {
  DEFAULT_USAGE_CONTEXT_PREFERENCES,
  readUsageContextPreferences,
  usageContextLevelKey,
  usageContextSourceKey,
  writeUsageContextPreferences,
  type UsageContextOverride,
} from "@/lib/usageContextPreferences";

const mocks = vi.hoisted(() => ({
  conversationId: "session-1" as string | null,
  agentName: "claude" as string | null,
  harness: "claude-native" as string | null,
  model: "claude-opus-5-5" as string | null,
  contextWindow: 330_000 as number | null,
  autoCompactTokenLimit: null as number | null,
  providerUsageLimits: null as ProviderUsageLimitsSnapshot | null,
  registryItems: [] as RegistryItem[],
  hosts: [] as Host[],
  sessionHostId: "host-a" as string | null,
}));

vi.mock("@/store/chatStore", () => ({
  useChatStore: (selector: (state: Record<string, unknown>) => unknown) =>
    selector({
      conversationId: mocks.conversationId,
      boundAgentName: mocks.agentName,
      sessionHarness: mocks.harness,
      llmModel: mocks.model,
      contextWindow: mocks.contextWindow,
      autoCompactTokenLimit: mocks.autoCompactTokenLimit,
      providerUsageLimits: mocks.providerUsageLimits,
    }),
}));

vi.mock("@/hooks/useSession", () => ({
  useSession: () => ({
    session: mocks.sessionHostId === null ? null : { hostId: mocks.sessionHostId },
    isLoading: false,
    error: null,
  }),
}));

vi.mock("@/hooks/useAgentRegistry", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  useAgentRegistry: () => ({ items: mocks.registryItems }),
}));

vi.mock("@/hooks/useHosts", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  useHosts: () => ({ data: mocks.hosts }),
  useCodexRateLimits: () => ({ data: null, isLoading: false }),
}));

import { ContextUsageSettings } from "./ContextUsageSettings";

const POLY: RegistryItem = {
  key: "agent-claude",
  name: "claude",
  label: "Claude Code",
  section: "yours",
  harness: "claude-native",
  description: null,
  deployed: true,
  source: "library",
};
const CODEX: RegistryItem = {
  key: "harness:codex",
  name: "codex",
  label: "Codex",
  section: "sdk",
  harness: "codex",
  description: null,
  deployed: true,
  source: "builtin",
};

const HOSTS: Host[] = [
  { host_id: "host-a", name: "Alpha", owner: "alice", status: "online", platform: "darwin" },
  {
    host_id: "host-win",
    name: "Windows box",
    owner: "alice",
    status: "online",
    platform: "win32",
    configured_harnesses: { "claude-native": true },
  },
];

const source = { hostId: "host-a", agentName: "claude", harness: "claude-native", model: "" };

function value(contextWindowTokens: number | null, buffer: number | null = null) {
  return { contextWindowTokens, autoCompactBufferTokens: buffer };
}

function store(overrides: Record<string, UsageContextOverride>) {
  writeUsageContextPreferences({ ...DEFAULT_USAGE_CONTEXT_PREFERENCES, overrides });
}

function LocationProbe() {
  const location = useLocation();
  return (
    <span data-testid="location">
      {location.pathname}
      {location.search}
      {location.hash}
    </span>
  );
}

function renderSettings(entry = "/settings/general#context-usage") {
  return render(
    <MemoryRouter initialEntries={[entry]}>
      <ContextUsageSettings />
      <LocationProbe />
    </MemoryRouter>,
  );
}

function cell(row: string, col: string) {
  return screen.getByTestId(`layered-cell-${row}-${col}`);
}

beforeEach(() => {
  localStorage.clear();
  mocks.conversationId = "session-1";
  mocks.agentName = "claude";
  mocks.harness = "claude-native";
  mocks.model = "claude-opus-5-5";
  mocks.contextWindow = 330_000;
  mocks.autoCompactTokenLimit = null;
  mocks.providerUsageLimits = null;
  mocks.registryItems = [POLY, CODEX];
  mocks.hosts = HOSTS;
  mocks.sessionHostId = "host-a";
});

describe("ContextUsageSettings", () => {
  it("names the agents with their integration mark and shows the saved value in the cell", () => {
    store({ [usageContextLevelKey(source, "cell")]: value(300_000, 20_000) });
    renderSettings();
    console.log("CELL:", cell("claude-claude-native", "host-a").outerHTML);

    const table = screen.getByRole("table", { name: "Saved context values" });
    expect(within(table).getByText("Claude Code")).toBeInTheDocument();
    expect(within(table).getByText("Codex")).toBeInTheDocument();
    expect(cell("claude-claude-native", "host-a")).toHaveTextContent("300k · 20k");
    expect(cell("claude-claude-native", "host-a").querySelector(".bg-pink-500")).not.toBeNull();
    // A cell with nothing anywhere shows what the agent reports, in italics.
    expect(cell("codex-codex", "host-a")).toHaveTextContent("Auto");
    expect(cell("codex-codex", "host-a").querySelector(".italic")).not.toBeNull();
  });

  it("shows the corner on a row with no value of its own anywhere", () => {
    store({
      [usageContextLevelKey({ ...source, hostId: "", agentName: "", harness: "" }, "all")]:
        value(550_000),
    });
    renderSettings();

    expect(cell("claude-claude-native", "host-a")).toHaveTextContent("550k · Auto");
    // Inherited from a level above, so it carries the arrow and stays a button.
    expect(cell("claude-claude-native", "host-a").querySelector("svg")).not.toBeNull();
    expect(cell("claude-claude-native", "host-a").querySelector(".bg-pink-500")).toBeNull();
    expect(screen.getByRole("button", { name: /Edit All agents, all hosts/ })).toHaveTextContent(
      "550k · Auto",
    );
  });

  it("saves a cell against the level it inherits and drops a value that repeats it", () => {
    store({ [usageContextLevelKey(source, "column")]: value(400_000) });
    renderSettings();

    fireEvent.click(cell("claude-claude-native", "host-a"));
    const input = screen.getByLabelText("Context total for Alpha × Claude Code");
    expect(screen.getByText("Context total: From Claude Code, all hosts")).toBeInTheDocument();

    fireEvent.change(input, { target: { value: "400000" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(
      readUsageContextPreferences().overrides[usageContextLevelKey(source, "cell")],
    ).toBeUndefined();

    fireEvent.change(input, { target: { value: "300000" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(
      readUsageContextPreferences().overrides[usageContextLevelKey(source, "cell")],
    ).toMatchObject({ contextWindowTokens: 300_000 });
    expect(cell("claude-claude-native", "host-a")).toHaveTextContent("300k · Auto");
  });

  it("orders the cell after its own value, then the agent, then the computer, then all", () => {
    renderSettings();
    fireEvent.click(cell("claude-claude-native", "host-a"));
    expect(
      screen.getByText(
        "This cell, then Claude Code, all hosts, then all agents on Alpha, then all agents, all hosts.",
      ),
    ).toBeInTheDocument();
  });

  it("counts the model values saved for one cell", () => {
    store({
      [usageContextSourceKey({ ...source, hostId: "host-a", model: "claude-opus-5-5" })]:
        value(100_000),
      [usageContextSourceKey({ ...source, hostId: "host-a", model: "claude-sonnet-5" })]:
        value(200_000),
    });
    renderSettings();
    expect(cell("claude-claude-native", "host-a")).toHaveTextContent("2 models");
  });

  it("marks a cell whose two headers disagree and names the winner", () => {
    store({
      [usageContextLevelKey(source, "column")]: value(400_000),
      [usageContextLevelKey({ ...source, agentName: "", harness: "" }, "host")]: value(500_000),
    });
    renderSettings();
    expect(
      within(cell("claude-claude-native", "host-a")).getByLabelText(
        "Warning: Context total: Claude Code and Alpha differ; Claude Code wins.",
      ),
    ).toBeInTheDocument();
  });

  it("shows a host where the agent cannot run as text, not a button", () => {
    renderSettings();
    expect(screen.getByText("Not on Windows")).toBeInTheDocument();
    expect(screen.getByText("Not installed here")).toBeInTheDocument();
  });

  it("opens this session's row on its model and clears ctx from the URL", async () => {
    const exact = usageContextSourceKey({ ...source, hostId: "host-a", model: "claude-opus-5-5" });
    renderSettings(`/settings/general?ctx=${encodeURIComponent(exact)}#context-usage`);

    const input = await screen.findByLabelText("Context total for claude-opus-5-5");
    expect(input).toHaveFocus();
    expect(screen.getByText(/this session's model/)).toBeInTheDocument();
    expect(screen.getByRole("dialog")).toHaveTextContent("Claude Code, all hosts");
    await waitFor(() =>
      expect(screen.getByTestId("location")).toHaveTextContent("/settings/general#context-usage"),
    );
    expect(screen.getByTestId("location")).not.toHaveTextContent("ctx=");
  });

  it("focuses the all-models fields when the session has no model", async () => {
    const exact = usageContextSourceKey({ ...source, model: "" });
    renderSettings(`/settings/general?ctx=${encodeURIComponent(exact)}#context-usage`);
    expect(
      await screen.findByLabelText("Context total for all models of Claude Code"),
    ).toHaveFocus();
    expect(screen.queryByLabelText(/this session's model/)).not.toBeInTheDocument();
  });

  it("says so and opens nothing when the session's agent has no row", async () => {
    const exact = usageContextSourceKey({
      hostId: "host-a",
      agentName: "gone",
      harness: "pi",
      model: "m",
    });
    renderSettings(`/settings/general?ctx=${encodeURIComponent(exact)}#context-usage`);
    expect(await screen.findByTestId("context-missing-source")).toHaveTextContent(
      "This session's agent is not listed here.",
    );
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.getByRole("table", { name: "Saved context values" })).toBeInTheDocument();
  });

  it("carries a typed value one step down to the cell without saving it", async () => {
    renderSettings(
      `/settings/general?ctx=${encodeURIComponent(usageContextSourceKey({ ...source, model: "" }))}#context-usage`,
    );
    const allModels = await screen.findByLabelText("Context total for all models of Claude Code");
    fireEvent.change(allModels, { target: { value: "250000" } });

    fireEvent.click(screen.getByRole("button", { name: "Only on Alpha" }));
    const cellInput = await screen.findByLabelText("Context total for Claude Code on Alpha");
    expect(cellInput).toHaveValue(250_000);
    expect(readUsageContextPreferences().overrides).toEqual({});
    // The row's own value is still unsaved, and the panel stays open.
    expect(screen.getByLabelText("Context total for all models of Claude Code")).toHaveValue(
      250_000,
    );

    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(
      readUsageContextPreferences().overrides[usageContextLevelKey(source, "cell")],
    ).toMatchObject({ contextWindowTokens: 250_000 });
  });

  it("lists a model value in the agent row with its scope and removes it", async () => {
    const modelKey = usageContextLevelKey({ ...source, model: "claude-opus-5-5" }, "agent-model");
    store({ [modelKey]: value(200_000) });
    renderSettings(
      `/settings/general?ctx=${encodeURIComponent(usageContextSourceKey({ ...source, model: "claude-opus-5-5" }))}#context-usage`,
    );

    expect(await screen.findByText("Saved for one model")).toBeInTheDocument();
    expect(screen.getAllByText("claude-opus-5-5").length).toBeGreaterThan(0);
    fireEvent.click(
      screen.getByRole("button", {
        name: "Remove the value for claude-opus-5-5 on Every computer",
      }),
    );
    await waitFor(() => expect(readUsageContextPreferences().overrides[modelKey]).toBeUndefined());
  });

  it("applies one value to the selected cells and undoes it", async () => {
    store({ [usageContextLevelKey(source, "cell")]: value(100_000) });
    renderSettings();

    fireEvent.click(screen.getByRole("button", { name: "Select cells" }));
    fireEvent.click(screen.getByTestId("layered-row-claude-claude-native"));
    fireEvent.click(screen.getByRole("button", { name: "Set values" }));
    fireEvent.change(screen.getByLabelText("Context total for the selection"), {
      target: { value: "600000" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Apply to 1 selected" }));
    expect(
      readUsageContextPreferences().overrides[usageContextLevelKey(source, "cell")],
    ).toMatchObject({ contextWindowTokens: 600_000 });

    fireEvent.click(screen.getByRole("button", { name: "Undo" }));
    expect(
      readUsageContextPreferences().overrides[usageContextLevelKey(source, "cell")],
    ).toMatchObject({ contextWindowTokens: 100_000 });
  });

  it("keeps the usage limits row and shows no provider reading without a harness session", () => {
    mocks.harness = null;
    renderSettings();
    expect(screen.getByTestId("provider-usage-source-status")).toHaveTextContent("Open a session");
    expect(screen.getByLabelText("Show usage limits")).toBeInTheDocument();
  });

  it("undoes only the records changed by Clear values below", async () => {
    const cellKey = usageContextLevelKey(source, "cell");
    const columnKey = usageContextLevelKey(source, "column");
    store({ [cellKey]: value(100_000) });
    renderSettings();

    fireEvent.click(screen.getByTestId("layered-row-claude-claude-native"));
    fireEvent.click(await screen.findByRole("button", { name: "Clear 1 value" }));
    await waitFor(() => expect(readUsageContextPreferences().overrides[cellKey]).toBeUndefined());

    fireEvent.change(screen.getByLabelText("Context total for all models of Claude Code"), {
      target: { value: "250000" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save all models" }));
    expect(readUsageContextPreferences().overrides[columnKey]).toMatchObject({
      contextWindowTokens: 250_000,
    });

    fireEvent.click(screen.getByRole("button", { name: "Undo" }));
    await waitFor(() =>
      expect(readUsageContextPreferences().overrides[cellKey]).toMatchObject({
        contextWindowTokens: 100_000,
      }),
    );
    expect(readUsageContextPreferences().overrides[columnKey]).toMatchObject({
      contextWindowTokens: 250_000,
    });
  });

  it("keeps a repeated buffer when the proposed cell total activates another buffer", () => {
    const cellKey = usageContextLevelKey(source, "cell");
    const corner = { hostId: "", agentName: "", harness: "", model: "" };
    store({
      [usageContextLevelKey(source, "column")]: value(100_000, 150_000),
      [usageContextLevelKey(corner, "all")]: value(null, 20_000),
    });
    renderSettings();

    fireEvent.click(cell("claude-claude-native", "host-a"));
    fireEvent.change(screen.getByLabelText("Context total for Alpha × Claude Code"), {
      target: { value: "200000" },
    });
    fireEvent.change(screen.getByLabelText("Compact buffer for Alpha × Claude Code"), {
      target: { value: "20000" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    expect(readUsageContextPreferences().overrides[cellKey]).toMatchObject({
      contextWindowTokens: 200_000,
      autoCompactBufferTokens: 20_000,
      compactBufferMode: "tokens",
    });
  });

  it("preserves Percent mode when its empty field retains an inactive token buffer", () => {
    const cellKey = usageContextLevelKey(source, "cell");
    store({
      [cellKey]: {
        contextWindowTokens: 100_000,
        autoCompactBufferTokens: 20_000,
        compactBufferMode: "percent",
      },
    });
    renderSettings();

    fireEvent.click(cell("claude-claude-native", "host-a"));
    expect(screen.getByLabelText("Compact buffer for Alpha × Claude Code")).toHaveValue(null);
    fireEvent.change(screen.getByLabelText("Context total for Alpha × Claude Code"), {
      target: { value: "200000" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    expect(readUsageContextPreferences().overrides[cellKey]).toMatchObject({
      contextWindowTokens: 200_000,
      autoCompactBufferTokens: 20_000,
      compactBufferMode: "percent",
    });
  });

  it("deduplicates each bulk-saved cell against its own inherited value", () => {
    const cellKey = usageContextLevelKey(source, "cell");
    const corner = { hostId: "", agentName: "", harness: "", model: "" };
    store({ [usageContextLevelKey(corner, "all")]: value(300_000) });
    renderSettings();

    fireEvent.click(screen.getByRole("button", { name: "Select cells" }));
    fireEvent.click(screen.getByTestId("layered-row-claude-claude-native"));
    fireEvent.click(screen.getByRole("button", { name: "Set values" }));
    fireEvent.change(screen.getByLabelText("Context total for the selection"), {
      target: { value: "300000" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Apply to 2 selected" }));

    expect(readUsageContextPreferences().overrides[cellKey]).toBeUndefined();
  });

  it("drills a focused model to its exact host record with the model draft", async () => {
    const model = "claude-opus-5-5";
    const exactKey = usageContextSourceKey({ ...source, model });
    renderSettings(`/settings/general?ctx=${encodeURIComponent(exactKey)}#context-usage`);

    fireEvent.change(await screen.findByLabelText(`Context total for ${model}`), {
      target: { value: "420000" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Only on Alpha" }));
    const drilled = await screen.findByLabelText("Context total for Claude Code on Alpha");
    expect(drilled).toHaveValue(420_000);
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    expect(readUsageContextPreferences().overrides[exactKey]).toMatchObject({
      contextWindowTokens: 420_000,
    });
    expect(
      readUsageContextPreferences().overrides[usageContextLevelKey(source, "cell")],
    ).toBeUndefined();
  });

  it("names the validated buffer winner in a crossing clash", () => {
    store({
      [usageContextLevelKey(source, "cell")]: value(100_000),
      [usageContextLevelKey(source, "column")]: value(null, 150_000),
      [usageContextLevelKey({ ...source, agentName: "", harness: "" }, "host")]: value(
        null,
        20_000,
      ),
    });
    renderSettings();

    expect(
      within(cell("claude-claude-native", "host-a")).getByLabelText(
        "Warning: Compact buffer: Claude Code and Alpha differ; Alpha wins.",
      ),
    ).toBeInTheDocument();
  });

  it("counts and clears model records below the corner", async () => {
    const cellKey = usageContextLevelKey(source, "cell");
    const modelKey = usageContextLevelKey({ ...source, model: "claude-opus-5-5" }, "agent-model");
    store({ [cellKey]: value(100_000), [modelKey]: value(200_000) });
    renderSettings();

    fireEvent.click(screen.getByRole("button", { name: /Edit All agents, all hosts/ }));
    fireEvent.click(await screen.findByRole("button", { name: "Clear 2 values" }));

    await waitFor(() => {
      expect(readUsageContextPreferences().overrides[cellKey]).toBeUndefined();
      expect(readUsageContextPreferences().overrides[modelKey]).toBeUndefined();
    });
  });

  it("does not mark a reported reading as a value saved in the cell", () => {
    mocks.contextWindow = 128_000;
    renderSettings();

    expect(cell("claude-claude-native", "host-a")).toHaveTextContent("128k · Auto");
    expect(cell("claude-claude-native", "host-a").querySelector(".bg-pink-500")).toBeNull();
  });
});
