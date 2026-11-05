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
  type UsageContextListEntry,
  type UsageContextOverride,
  type UsageContextPreferences,
} from "@/lib/usageContextPreferences";

const mocks = vi.hoisted(() => ({
  conversationId: "session-1" as string | null,
  agentName: "claude" as string | null,
  harness: "claude-native" as string | null,
  model: "claude-opus-5-5" as string | null,
  providerUsageLimits: null as ProviderUsageLimitsSnapshot | null,
  registryItems: [] as RegistryItem[],
  hosts: [] as Host[],
  sessionHostId: "host-a" as string | null,
  sessionAgentIds: new Set<string>() as Set<string> | undefined,
}));

vi.mock("@/store/chatStore", () => ({
  useChatStore: (selector: (state: Record<string, unknown>) => unknown) =>
    selector({
      conversationId: mocks.conversationId,
      boundAgentName: mocks.agentName,
      sessionHarness: mocks.harness,
      llmModel: mocks.model,
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
  useAgentRegistry: () => ({
    items: mocks.registryItems,
    legacyProducts: {},
    isLoading: false,
    isError: false,
  }),
  useAgentsWithSessions: () => ({ data: mocks.sessionAgentIds }),
}));

vi.mock("@/hooks/useHosts", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  useHosts: () => ({ data: mocks.hosts }),
  useCodexRateLimits: () => ({ data: null, isLoading: false }),
}));

import { ContextUsageSettings } from "./ContextUsageSettings";

const CLAUDE: RegistryItem = {
  key: "harness:claude-native",
  name: "claude",
  label: "Claude Code",
  section: "native",
  harness: "claude-native",
  description: null,
  deployed: true,
  source: "builtin",
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
const CURSOR: RegistryItem = {
  key: "harness:cursor-native",
  name: "cursor",
  label: "Cursor",
  section: "native",
  harness: "cursor-native",
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

const AGENT: UsageContextListEntry = { agentName: "claude", harness: "claude-native", model: "" };
const OPUS: UsageContextListEntry = { ...AGENT, model: "claude-opus-5-5" };
const SONNET: UsageContextListEntry = { ...AGENT, model: "claude-sonnet-5-5" };
const CODEX_AGENT: UsageContextListEntry = { agentName: "codex", harness: "codex", model: "" };
const GPT: UsageContextListEntry = { ...CODEX_AGENT, model: "gpt-x" };

const ALL = "all-computers";
const CLAUDE_ROW = "claude-claude-native";
const OPUS_ROW = "claude-claude-native-claude-opus-5-5";
const SONNET_ROW = "claude-claude-native-claude-sonnet-5-5";
const CODEX_ROW = "codex-codex";
const GPT_ROW = "codex-codex-gpt-x";

const agentSource = { hostId: "host-a", agentName: "claude", harness: "claude-native", model: "" };

function value(contextWindowTokens: number | null, buffer: number | null = null) {
  return { contextWindowTokens, autoCompactBufferTokens: buffer };
}

function store(
  entries: readonly UsageContextListEntry[],
  overrides: Record<string, UsageContextOverride> = {},
  extra: Partial<UsageContextPreferences> = {},
) {
  writeUsageContextPreferences({
    ...DEFAULT_USAGE_CONTEXT_PREFERENCES,
    contextList: { entries: [...entries], severalOpen: false },
    overrides,
    ...extra,
  });
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
  return screen.getByTestId(`context-cell-${row}-${col}`);
}

function slot(row: string, col: string, name: "total" | "buffer") {
  return cell(row, col).querySelector(`[data-context-slot="${name}"]`)!;
}

function expand(label: string) {
  fireEvent.click(screen.getByRole("button", { name: `Expand ${label}` }));
}

function saveFields(context: string, buffer?: string) {
  fireEvent.change(screen.getByRole("spinbutton", { name: /Context total/ }), {
    target: { value: context },
  });
  if (buffer !== undefined)
    fireEvent.change(screen.getByRole("spinbutton", { name: /Compact buffer/ }), {
      target: { value: buffer },
    });
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
}

beforeEach(() => {
  localStorage.clear();
  mocks.conversationId = "session-1";
  mocks.agentName = "claude";
  mocks.harness = "claude-native";
  mocks.model = "claude-opus-5-5";
  mocks.providerUsageLimits = null;
  mocks.registryItems = [CLAUDE, CODEX, CURSOR];
  mocks.hosts = HOSTS;
  mocks.sessionHostId = "host-a";
  mocks.sessionAgentIds = new Set(["harness:claude-native", "harness:codex"]);
});

describe("ContextUsageSettings", () => {
  it("lists only the user's agents, with the computer columns", () => {
    store([AGENT]);
    renderSettings();
    const table = screen.getByRole("table", { name: "Context limits" });
    expect(within(table).getByText("Claude Code")).toBeInTheDocument();
    expect(within(table).queryByText("Codex")).not.toBeInTheDocument();
    expect(within(table).queryByText("Cursor")).not.toBeInTheDocument();
    for (const column of ["All computers", "Alpha", "Windows box"])
      expect(within(table).getByRole("columnheader", { name: new RegExp(column) })).toBeVisible();
  });

  it("seeds once from agents in use and recorded combinations, then leaves the list alone", () => {
    writeUsageContextPreferences({
      ...DEFAULT_USAGE_CONTEXT_PREFERENCES,
      lastProviderUsageLimits: {
        [usageContextSourceKey({ ...agentSource, model: OPUS.model })]: {
          provider: "Claude",
          scope: "account",
          capturedAt: 1,
          windows: [],
        } as unknown as ProviderUsageLimitsSnapshot,
      },
    });
    const view = renderSettings();
    expect(readUsageContextPreferences().contextList?.entries).toEqual([AGENT, CODEX_AGENT, OPUS]);
    expect(screen.queryByText("Cursor")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Remove Codex" }));
    view.unmount();
    renderSettings();
    expect(screen.queryByText("Codex")).not.toBeInTheDocument();
    expect(readUsageContextPreferences().contextList?.entries).toEqual([AGENT, OPUS]);
  });

  it("does not seed until it knows what is in use", () => {
    mocks.sessionAgentIds = undefined;
    renderSettings();
    expect(readUsageContextPreferences().contextList).toBeUndefined();
  });

  it("keeps a saved value visible as a row even when the list does not name it", () => {
    store([], {
      [usageContextLevelKey({ ...GPT, hostId: "" }, "agent-model")]: value(200_000),
    });
    renderSettings();
    expect(screen.getByText("Codex")).toBeInTheDocument();
    expand("Codex");
    expect(slot(GPT_ROW, ALL, "total")).toHaveTextContent("200k");
  });

  it("groups models under their agent and opening one group closes the other", () => {
    store([AGENT, OPUS, CODEX_AGENT, GPT]);
    renderSettings();
    expect(screen.queryByTestId(`context-row-${OPUS_ROW}`)).not.toBeInTheDocument();
    const group = screen.getByTestId(`context-group-${CLAUDE_ROW}`);
    expect(within(group).getByText("1 model")).toBeInTheDocument();

    expand("Claude Code");
    expect(screen.getByTestId(`context-row-${OPUS_ROW}`)).toBeInTheDocument();
    expect(within(group).getByText("All models")).toBeInTheDocument();

    expand("Codex");
    expect(screen.queryByTestId(`context-row-${OPUS_ROW}`)).not.toBeInTheDocument();
    expect(screen.getByTestId(`context-row-${GPT_ROW}`)).toBeInTheDocument();
  });

  it("keeps several groups open behind the switch, and trims to one when it goes off", () => {
    store([AGENT, OPUS, CODEX_AGENT, GPT]);
    renderSettings();
    fireEvent.click(screen.getByRole("switch", { name: "Keep several open" }));
    expect(readUsageContextPreferences().contextList?.severalOpen).toBe(true);
    expand("Claude Code");
    expand("Codex");
    expect(screen.getByTestId(`context-row-${OPUS_ROW}`)).toBeInTheDocument();
    expect(screen.getByTestId(`context-row-${GPT_ROW}`)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("switch", { name: "Keep several open" }));
    expect(screen.queryByTestId(`context-row-${OPUS_ROW}`)).not.toBeInTheDocument();
    expect(screen.getByTestId(`context-row-${GPT_ROW}`)).toBeInTheDocument();
  });

  it("offers no expander for an agent with no model rows", () => {
    store([AGENT]);
    renderSettings();
    expect(screen.queryByRole("button", { name: "Expand Claude Code" })).not.toBeInTheDocument();
  });

  it.each([
    ["all-agents", ALL, { hostId: "", agentName: "", harness: "", model: "" }, "all"],
    ["all-agents", "host-a", { hostId: "host-a", agentName: "", harness: "", model: "" }, "host"],
    [CLAUDE_ROW, ALL, { ...agentSource, hostId: "" }, "column"],
    [CLAUDE_ROW, "host-a", agentSource, "cell"],
    [OPUS_ROW, ALL, { ...OPUS, hostId: "" }, "agent-model"],
    [OPUS_ROW, "host-a", { ...OPUS, hostId: "host-a" }, "model"],
  ] as const)("writes %s × %s to its own level", (row, column, source, level) => {
    store([AGENT, OPUS]);
    renderSettings();
    expand("Claude Code");
    fireEvent.click(cell(row, column));
    saveFields("123000");
    expect(readUsageContextPreferences().overrides[usageContextLevelKey(source, level)]).toEqual(
      expect.objectContaining({ contextWindowTokens: 123_000 }),
    );
    expect(slot(row, column, "total")).toHaveTextContent("123k");
  });

  it("marks only the slot saved at this level, and shows inherited values without a mark", () => {
    store([AGENT, OPUS], {
      [usageContextLevelKey(agentSource, "cell")]: value(550_000),
    });
    renderSettings();
    expand("Claude Code");
    expect(slot(CLAUDE_ROW, "host-a", "total")).toHaveClass("font-semibold");
    expect(slot(CLAUDE_ROW, "host-a", "buffer")).not.toHaveClass("font-semibold");
    expect(slot(OPUS_ROW, "host-a", "total")).toHaveTextContent("550k");
    expect(slot(OPUS_ROW, "host-a", "total")).not.toHaveClass("font-semibold");
  });

  it("shows the agent's own value as Auto and offers Use Auto on the corner", () => {
    store(
      [AGENT, OPUS],
      { [usageContextLevelKey(agentSource, "all")]: value(200_000) },
      {
        reportedContext: {
          [usageContextSourceKey({ ...OPUS, hostId: "host-a" })]: {
            contextWindow: 1_000_000,
            autoCompactTokenLimit: 967_000,
            capturedAt: 1,
          },
        },
      },
    );
    renderSettings();
    expand("Claude Code");
    expect(slot("all-agents", ALL, "total")).toHaveTextContent("200k");

    fireEvent.click(cell("all-agents", ALL));
    fireEvent.click(screen.getByRole("button", { name: "Use Auto" }));
    fireEvent.keyDown(document.activeElement ?? document.body, { key: "Escape" });

    expect(slot("all-agents", ALL, "total")).toHaveTextContent("Auto");
    expect(slot(OPUS_ROW, "host-a", "total")).toHaveTextContent("1M");
    expect(slot(OPUS_ROW, "host-a", "total")).toHaveClass("italic");
    expect(slot(OPUS_ROW, "host-a", "buffer")).toHaveTextContent("33k");
    expect(cell(OPUS_ROW, "host-a").title).toContain("compacts at 967000 tokens");
  });

  it("flags a saved value that disagrees with the report, here and on the collapsed group", () => {
    store(
      [AGENT, OPUS],
      { [usageContextLevelKey(agentSource, "cell")]: value(550_000, 32_000) },
      {
        reportedContext: {
          [usageContextSourceKey({ ...OPUS, hostId: "host-a" })]: {
            contextWindow: 1_000_000,
            autoCompactTokenLimit: 967_000,
            capturedAt: 1,
          },
        },
      },
    );
    renderSettings();
    expect(
      screen.getByRole("img", { name: "A model here differs from what the agent reports" }),
    ).toBeInTheDocument();
    expand("Claude Code");
    const warning = within(screen.getByTestId(`context-row-${OPUS_ROW}`)).getByRole("button", {
      name: "Warning: Context 550k here; the agent reports 1M.\nCompacts at 518k here; the agent reports 967k.",
    });
    expect(warning.closest("td")).toContainElement(cell(OPUS_ROW, "host-a"));
  });

  it("does not flag a saved value that matches the report", () => {
    store(
      [AGENT, OPUS],
      { [usageContextLevelKey(agentSource, "cell")]: value(1_000_000, 33_000) },
      {
        reportedContext: {
          [usageContextSourceKey({ ...OPUS, hostId: "host-a" })]: {
            contextWindow: 1_000_000,
            autoCompactTokenLimit: 967_000,
            capturedAt: 1,
          },
        },
      },
    );
    renderSettings();
    expand("Claude Code");
    expect(screen.queryByRole("button", { name: /^Warning:/ })).not.toBeInTheDocument();
  });

  it("does not flag an agent row when its models report different sizes", () => {
    store(
      [AGENT, OPUS, SONNET],
      { [usageContextLevelKey(agentSource, "column")]: value(550_000) },
      {
        reportedContext: {
          [usageContextSourceKey({ ...OPUS, hostId: "host-a" })]: {
            contextWindow: 1_000_000,
            autoCompactTokenLimit: null,
            capturedAt: 1,
          },
          [usageContextSourceKey({ ...SONNET, hostId: "host-a" })]: {
            contextWindow: 200_000,
            autoCompactTokenLimit: null,
            capturedAt: 2,
          },
        },
      },
    );
    renderSettings();
    expect(
      within(screen.getByTestId(`context-row-${CLAUDE_ROW}`)).queryByRole("button", {
        name: /^Warning:/,
      }),
    ).not.toBeInTheDocument();
    expand("Claude Code");
    expect(screen.getAllByRole("button", { name: /^Warning:/ }).length).toBeGreaterThan(0);
  });

  it("shows the compact point while typing, beside what the agent reports", () => {
    store(
      [AGENT, OPUS],
      {},
      {
        reportedContext: {
          [usageContextSourceKey({ ...OPUS, hostId: "host-a" })]: {
            contextWindow: 1_000_000,
            autoCompactTokenLimit: 967_000,
            capturedAt: 1,
          },
        },
      },
    );
    renderSettings();
    expand("Claude Code");
    fireEvent.click(cell(OPUS_ROW, "host-a"));
    expect(screen.getByTestId("context-compact-point")).toHaveTextContent("Compacts at 967k");
    fireEvent.change(screen.getByRole("spinbutton", { name: /Compact buffer/ }), {
      target: { value: "50000" },
    });
    expect(screen.getByTestId("context-compact-point")).toHaveTextContent(
      "Compacts at 950k · the agent reports 967k",
    );
  });

  it("adds a recorded combination into its agent's group from the bottom picker", async () => {
    store(
      [AGENT, CODEX_AGENT],
      {},
      {
        reportedContext: {
          [usageContextSourceKey({ ...SONNET, hostId: "host-a" })]: {
            contextWindow: 200_000,
            autoCompactTokenLimit: null,
            capturedAt: 1,
          },
        },
      },
    );
    renderSettings();
    fireEvent.click(screen.getByTestId("context-add-entry"));
    const picker = await screen.findByTestId("context-add-picker");
    fireEvent.click(within(picker).getByText("claude-sonnet-5-5"));

    expect(readUsageContextPreferences().contextList?.entries).toContainEqual(SONNET);
    const group = screen.getByTestId(`context-group-${CLAUDE_ROW}`);
    expect(within(group).getByTestId(`context-row-${SONNET_ROW}`)).toBeInTheDocument();
  });

  it("adds an agent in use that is not listed, and never one that is not in use", async () => {
    store([AGENT]);
    renderSettings();
    fireEvent.click(screen.getByTestId("context-add-entry"));
    const picker = await screen.findByTestId("context-add-picker");
    expect(within(picker).queryByText("Cursor")).not.toBeInTheDocument();
    expect(within(picker).queryByText("Claude Code")).not.toBeInTheDocument();
    fireEvent.click(within(picker).getByText("Codex"));
    expect(readUsageContextPreferences().contextList?.entries).toContainEqual(CODEX_AGENT);
    expect(screen.getByTestId(`context-row-${CODEX_ROW}`)).toBeInTheDocument();
  });

  it("adds a typed model to the agent the user picks", async () => {
    store([AGENT]);
    renderSettings();
    fireEvent.click(screen.getByTestId("context-add-entry"));
    const picker = await screen.findByTestId("context-add-picker");
    fireEvent.change(within(picker).getByRole("combobox"), { target: { value: "brand-new" } });
    const group = await within(picker).findByText("Add brand-new to");
    fireEvent.click(within(group.closest("[cmdk-group]") as HTMLElement).getByText("Claude Code"));
    expect(readUsageContextPreferences().contextList?.entries).toContainEqual({
      ...AGENT,
      model: "brand-new",
    });
    expect(screen.getByText("brand-new")).toBeInTheDocument();
  });

  it("removes a group with its saved values and undoes it, list and values together", () => {
    const opusKey = usageContextLevelKey({ ...OPUS, hostId: "" }, "agent-model");
    const cellKey = usageContextLevelKey(agentSource, "cell");
    const codexKey = usageContextLevelKey({ ...CODEX_AGENT, hostId: "" }, "column");
    store([AGENT, OPUS, CODEX_AGENT], {
      [opusKey]: value(200_000),
      [cellKey]: value(300_000),
      [codexKey]: value(400_000),
    });
    renderSettings();
    fireEvent.click(screen.getByRole("button", { name: "Remove Claude Code" }));

    const after = readUsageContextPreferences();
    expect(after.contextList?.entries).toEqual([CODEX_AGENT]);
    expect(Object.keys(after.overrides)).toEqual([codexKey]);
    expect(screen.queryByText("Claude Code")).not.toBeInTheDocument();
    const status = screen.getByTestId("context-undo-status");
    expect(status).toHaveTextContent("Removed Claude Code and 2 saved values.");

    fireEvent.click(within(status).getByRole("button", { name: "Undo" }));
    const restored = readUsageContextPreferences();
    expect(restored.contextList?.entries).toHaveLength(3);
    expect(restored.contextList?.entries).toEqual(
      expect.arrayContaining([AGENT, OPUS, CODEX_AGENT]),
    );
    expect(restored.overrides[opusKey]).toEqual(value(200_000));
    expect(restored.overrides[cellKey]).toEqual(value(300_000));
    expect(screen.getByText("Claude Code")).toBeInTheDocument();
  });

  it("undoes a removal without dropping a row added after it", async () => {
    store([AGENT, CODEX_AGENT]);
    renderSettings();
    fireEvent.click(screen.getByRole("button", { name: "Remove Codex" }));
    const status = screen.getByTestId("context-undo-status");

    fireEvent.click(screen.getByTestId("context-add-entry"));
    const picker = await screen.findByTestId("context-add-picker");
    fireEvent.change(within(picker).getByRole("combobox"), { target: { value: "brand-new" } });
    const group = await within(picker).findByText("Add brand-new to");
    fireEvent.click(within(group.closest("[cmdk-group]") as HTMLElement).getByText("Claude Code"));

    fireEvent.click(within(status).getByRole("button", { name: "Undo" }));
    const entries = readUsageContextPreferences().contextList?.entries;
    expect(entries).toHaveLength(3);
    expect(entries).toEqual(
      expect.arrayContaining([AGENT, CODEX_AGENT, { ...AGENT, model: "brand-new" }]),
    );
  });

  it("removes one model row and only that model's values", () => {
    const opusKey = usageContextLevelKey({ ...OPUS, hostId: "" }, "agent-model");
    const sonnetKey = usageContextLevelKey({ ...SONNET, hostId: "" }, "agent-model");
    store([AGENT, OPUS, SONNET], { [opusKey]: value(200_000), [sonnetKey]: value(300_000) });
    renderSettings();
    expand("Claude Code");
    fireEvent.click(screen.getByRole("button", { name: "Remove Claude Code · claude-opus-5-5" }));
    const after = readUsageContextPreferences();
    expect(after.contextList?.entries).toEqual([AGENT, SONNET]);
    expect(Object.keys(after.overrides)).toEqual([sonnetKey]);
  });

  it("drops a cell's value that repeats what it inherits", () => {
    store([AGENT], { [usageContextLevelKey(agentSource, "column")]: value(400_000) });
    renderSettings();
    fireEvent.click(cell(CLAUDE_ROW, "host-a"));
    saveFields("400000");
    expect(
      readUsageContextPreferences().overrides[usageContextLevelKey(agentSource, "cell")],
    ).toBeUndefined();
    expect(slot(CLAUDE_ROW, "host-a", "total")).toHaveTextContent("400k");
  });

  it("saves on Enter and closes, and shows an error without closing", () => {
    store([AGENT]);
    renderSettings();
    fireEvent.click(cell(CLAUDE_ROW, "host-a"));
    const total = screen.getByRole("spinbutton", { name: /Context total/ });
    fireEvent.change(total, { target: { value: "100000" } });
    fireEvent.change(screen.getByRole("spinbutton", { name: /Compact buffer/ }), {
      target: { value: "200000" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(screen.getByRole("alert")).toHaveTextContent(
      "Token buffer must be less than the context total.",
    );
    fireEvent.change(screen.getByRole("spinbutton", { name: /Compact buffer/ }), {
      target: { value: "20000" },
    });
    fireEvent.keyDown(total, { key: "Enter" });
    expect(screen.queryByRole("spinbutton", { name: /Context total/ })).not.toBeInTheDocument();
    expect(slot(CLAUDE_ROW, "host-a", "buffer")).toHaveTextContent("20k");
  });

  it("drops a typed value on Cancel", () => {
    store([AGENT]);
    renderSettings();
    fireEvent.click(cell(CLAUDE_ROW, ALL));
    fireEvent.change(screen.getByRole("spinbutton", { name: /Context total/ }), {
      target: { value: "100000" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(readUsageContextPreferences().overrides).toEqual({});
  });

  it("keeps a hand-entered percentage and an entered number that shorthand would round", () => {
    store([AGENT], {
      [usageContextLevelKey(agentSource, "cell")]: {
        contextWindowTokens: 123_456,
        autoCompactBufferTokens: null,
        autoCompactBufferPercent: 10,
        compactBufferMode: "percent",
      },
    });
    renderSettings();
    expect(slot(CLAUDE_ROW, "host-a", "total")).toHaveTextContent("123456");
    expect(slot(CLAUDE_ROW, "host-a", "buffer")).toHaveTextContent("10%");
  });

  it("marks a cell whose two headers disagree and names the winner", () => {
    store([AGENT], {
      [usageContextLevelKey(agentSource, "column")]: value(400_000),
      [usageContextLevelKey(agentSource, "host")]: value(350_000),
    });
    renderSettings();
    expect(
      screen.getByRole("button", {
        name: "Warning: Context total: Claude Code and Alpha differ; Claude Code wins.",
      }),
    ).toBeInTheDocument();
  });

  it("names a buffer ignored for not fitting the context total", () => {
    store([AGENT], {
      [usageContextLevelKey(agentSource, "cell")]: value(100_000, 150_000),
    });
    renderSettings();
    expect(
      screen.getByRole("button", {
        name: "Warning: The buffer saved here is ignored: it is not below the context total.",
      }),
    ).toBeInTheDocument();
  });

  it("shows a computer where the agent cannot run as text, not a button", () => {
    store([AGENT]);
    renderSettings();
    expect(screen.getByText("Not on Windows")).toBeInTheDocument();
    expect(screen.queryByTestId(`context-cell-${CLAUDE_ROW}-host-win`)).not.toBeInTheDocument();
  });

  it("opens the ring's model row on all computers, adds it, and clears ctx from the URL", async () => {
    store([AGENT]);
    const key = usageContextSourceKey({ ...OPUS, hostId: "host-a" });
    renderSettings(`/settings/general?ctx=${encodeURIComponent(key)}#context-usage`);

    await screen.findByRole("spinbutton", { name: /Context total/ });
    expect(readUsageContextPreferences().contextList?.entries).toContainEqual(OPUS);
    expect(cell(OPUS_ROW, ALL)).toHaveAttribute("aria-expanded", "true");
    await waitFor(() =>
      expect(document.activeElement).toBe(
        screen.getByRole("spinbutton", { name: /Context total/ }),
      ),
    );
    expect(screen.getByTestId("location").textContent).not.toContain("ctx=");
  });

  it("opens the agent row on all computers when the session has no model", async () => {
    store([AGENT]);
    const key = usageContextSourceKey({ ...agentSource, model: "" });
    renderSettings(`/settings/general?ctx=${encodeURIComponent(key)}#context-usage`);
    await screen.findByRole("spinbutton", { name: /Context total/ });
    expect(cell(CLAUDE_ROW, ALL)).toHaveAttribute("aria-expanded", "true");
  });

  it("lets the seed run before the ring's entry adds its row", async () => {
    const key = usageContextSourceKey({ ...OPUS, hostId: "host-a" });
    renderSettings(`/settings/general?ctx=${encodeURIComponent(key)}#context-usage`);
    await screen.findByRole("spinbutton", { name: /Context total/ });
    const entries = readUsageContextPreferences().contextList?.entries ?? [];
    expect(entries).toContainEqual(AGENT);
    expect(entries).toContainEqual(CODEX_AGENT);
    expect(entries).toContainEqual(OPUS);
  });

  it("says so when the ring's entry names no agent", async () => {
    store([AGENT]);
    renderSettings(`/settings/general?ctx=${encodeURIComponent('["","","",""]')}#context-usage`);
    expect(await screen.findByTestId("context-missing-source")).toBeInTheDocument();
    expect(screen.queryByRole("spinbutton", { name: /Context total/ })).not.toBeInTheDocument();
  });

  it("counts and clears everything below the corner, with Undo", () => {
    const cellKey = usageContextLevelKey(agentSource, "cell");
    const modelKey = usageContextLevelKey({ ...OPUS, hostId: "host-a" }, "model");
    store([AGENT, OPUS], { [cellKey]: value(300_000), [modelKey]: value(200_000) });
    renderSettings();
    fireEvent.click(cell("all-agents", ALL));
    fireEvent.click(screen.getByRole("button", { name: "Clear 2 values" }));
    expect(readUsageContextPreferences().overrides).toEqual({});
    fireEvent.click(within(screen.getByTestId("context-undo-status")).getByRole("button"));
    expect(Object.keys(readUsageContextPreferences().overrides).sort()).toEqual(
      [cellKey, modelKey].sort(),
    );
  });

  it("clears only the values one computer's header reaches", () => {
    const hereKey = usageContextLevelKey(agentSource, "cell");
    const elsewhereKey = usageContextLevelKey({ ...agentSource, hostId: "host-b" }, "cell");
    const everyKey = usageContextLevelKey(agentSource, "column");
    store([AGENT], {
      [hereKey]: value(300_000),
      [elsewhereKey]: value(250_000),
      [everyKey]: value(200_000),
    });
    renderSettings();
    fireEvent.click(cell("all-agents", "host-a"));
    fireEvent.click(screen.getByRole("button", { name: "Clear 1 value" }));
    expect(Object.keys(readUsageContextPreferences().overrides).sort()).toEqual(
      [elsewhereKey, everyKey].sort(),
    );
  });

  it("keeps the usage limits row and shows no provider reading without a harness session", () => {
    mocks.harness = null;
    store([]);
    renderSettings();
    expect(screen.getByTestId("provider-usage-source-status")).toHaveTextContent("Open a session");
  });

  it("names an installed copy by its product's short name", () => {
    mocks.registryItems = [
      {
        ...CLAUDE,
        key: "agent-installed-copy",
        label: "claude · Claude Code · installed",
        section: "yours",
        source: "installed",
      },
      CLAUDE,
    ];
    store([AGENT]);
    renderSettings();
    expect(screen.getByText("Claude Code")).toBeInTheDocument();
  });
});
