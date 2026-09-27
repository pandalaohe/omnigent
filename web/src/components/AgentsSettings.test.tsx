import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { AvailableAgent } from "@/hooks/useAvailableAgents";
import {
  AGENT_BADGE_STORAGE_KEY,
  readAgentBadgePreferences,
  writeAgentBadgePreferences,
} from "@/lib/agentBadgePreferences";
import type { CustomAgent, CustomAgentDetail, CustomAgentMember } from "@/lib/customAgentsApi";
import { TooltipProvider } from "@/components/ui/tooltip";
import { AgentsSettings } from "./AgentsSettings";

const mocks = vi.hoisted(() => ({
  available: undefined as AvailableAgent[] | undefined,
  availableLoading: false,
  availableError: null as Error | null,
  catalog: [] as CustomAgent[],
  catalogLoading: false,
  catalogError: null as Error | null,
  refetch: vi.fn(),
  createCustomAgent: vi.fn(),
  deleteCustomAgent: vi.fn(),
  duplicateBuiltinAgent: vi.fn(),
  getCustomAgent: vi.fn(),
  importCustomAgent: vi.fn(),
  updateCustomAgent: vi.fn(),
  buildAgentBundle: vi.fn(),
  queueUserPreferencePatch: vi.fn(),
}));

vi.mock("@/hooks/useAvailableAgents", () => ({
  useAvailableAgents: () => ({
    data: mocks.available,
    isLoading: mocks.availableLoading,
    error: mocks.availableError,
  }),
}));

vi.mock("@/lib/customAgentsApi", () => ({
  CUSTOM_AGENTS_QUERY_KEY: ["custom-agents"],
  useCustomAgents: () => ({
    data: mocks.catalog,
    isLoading: mocks.catalogLoading,
    error: mocks.catalogError,
    refetch: mocks.refetch,
  }),
  createCustomAgent: mocks.createCustomAgent,
  customAgentForPicker: (agent: CustomAgent) => ({
    id: agent.id,
    name: agent.name,
    display_name: agent.name,
    description: agent.description,
    harness: agent.harness,
    skills: [],
    builtin: false,
    created_at: agent.created_at,
  }),
  deleteCustomAgent: mocks.deleteCustomAgent,
  duplicateBuiltinAgent: mocks.duplicateBuiltinAgent,
  getCustomAgent: mocks.getCustomAgent,
  importCustomAgent: mocks.importCustomAgent,
  updateCustomAgent: mocks.updateCustomAgent,
}));

vi.mock("@/lib/agentBundle", () => ({ buildAgentBundle: mocks.buildAgentBundle }));
vi.mock("@/lib/userPreferencesSync", () => ({
  queueUserPreferencePatch: mocks.queueUserPreferencePatch,
}));
vi.mock("@/lib/agentLabels", () => ({
  BRAIN_HARNESS_LABELS: { "claude-sdk": "Claude SDK" },
  useBrainHarnessLabels: () => ({ "claude-sdk": "Claude SDK" }),
}));
vi.mock("@/hooks/useHosts", () => ({
  useNewChatHostId: () => null,
  useHostModelOptions: () => ({ data: undefined }),
}));
vi.mock("@/lib/analytics", () => ({
  useOmnigentAnalytics: () => ({ trackValueChange: vi.fn() }),
}));
vi.mock("@/hooks/useSuppressBrowserView", () => ({ SuppressBrowserView: () => null }));

const builtin: AvailableAgent = {
  id: "ag_builtin_codex_sdk",
  name: "codex-sdk",
  display_name: "Codex SDK",
  description: "Codex chat agent",
  harness: "codex",
  skills: [],
  builtin: true,
  created_at: 1,
};

const custom: CustomAgent = {
  id: "ca_custom_reviewer",
  name: "Reviewer",
  description: "Reviews changes",
  harness: "codex",
  // The editor's lead requires a model (the omnigent executor rejects a root
  // without one), and this fixture drives the editor's Save.
  model: "opus",
  members: null,
  version: 3,
  created_at: 2,
  updated_at: null,
};

const customDetail: CustomAgentDetail = {
  ...custom,
  instructions: "Review carefully.",
};

const importable: AvailableAgent = {
  id: "ag_session_writer",
  name: "writer",
  display_name: "Writer",
  description: "Writes release notes",
  harness: "codex",
  skills: [],
  sessionId: "session_writer",
};

const leadMember: CustomAgentMember = {
  name: "polly",
  description: "Plans the work",
  harness: "claude-sdk",
  model: "opus",
  reasoning_effort: null,
  lead: true,
};

const builtinPolly: AvailableAgent = {
  id: "ag_builtin_polly",
  name: "polly",
  display_name: "Polly",
  description: "Multi-agent coding",
  harness: "claude-sdk",
  skills: [],
  builtin: true,
  members: [
    leadMember,
    {
      name: "codex",
      description: "Writes code",
      harness: "codex-native",
      model: null,
      reasoning_effort: "high",
      lead: false,
    },
    { ...leadMember, name: "reviewer", model: "sonnet", lead: false },
  ],
};

const builtinDebby: AvailableAgent = {
  id: "ag_builtin_debby",
  name: "debby",
  display_name: "Debby",
  description: "Multi-agent debate",
  harness: "claude-sdk",
  skills: [],
  builtin: true,
  members: [{ ...leadMember, name: "debby" }],
};

const builtinClaudeNative: AvailableAgent = {
  id: "ag_builtin_claude_native",
  name: "claude-native-ui",
  display_name: "Claude Code",
  description: "Anthropic's terminal coding agent",
  harness: "claude-native",
  skills: [],
  builtin: true,
};

const builtinAcp: AvailableAgent = {
  ...builtinClaudeNative,
  id: "ag_builtin_grok",
  name: "grok",
  display_name: "Grok",
  harness: "grok",
  acpHarness: true,
};

function renderSettings() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: Infinity } },
  });
  const invalidate = vi.spyOn(client, "invalidateQueries");
  const view = render(
    <QueryClientProvider client={client}>
      <TooltipProvider>
        <AgentsSettings />
      </TooltipProvider>
    </QueryClientProvider>,
  );
  return { ...view, invalidate };
}

beforeEach(() => {
  localStorage.clear();
  mocks.availableLoading = false;
  mocks.availableError = null;
  mocks.catalogLoading = false;
  mocks.catalogError = null;
  mocks.available = [
    builtin,
    builtinPolly,
    builtinDebby,
    importable,
    { ...importable, id: "ag_catalog_clone", sessionId: "session_clone", templateId: custom.id },
    {
      ...importable,
      id: "ag_orphaned_clone",
      display_name: "Orphaned clone",
      sessionId: "session_orphaned",
      templateId: "ca_deleted",
    },
  ];
  mocks.catalog = [custom];
  mocks.getCustomAgent.mockResolvedValue(customDetail);
  mocks.updateCustomAgent.mockResolvedValue(customDetail);
  mocks.deleteCustomAgent.mockResolvedValue(undefined);
  mocks.createCustomAgent.mockResolvedValue(customDetail);
  mocks.duplicateBuiltinAgent.mockResolvedValue(customDetail);
  mocks.importCustomAgent.mockResolvedValue(customDetail);
  mocks.buildAgentBundle.mockResolvedValue(
    new File(["bundle"], "agent.tar.gz", { type: "application/gzip" }),
  );
});

afterEach(() => {
  cleanup();
  localStorage.clear();
  vi.clearAllMocks();
});

describe("AgentsSettings", () => {
  it("adds and removes an optional badge on a built-in Agent", async () => {
    renderSettings();

    fireEvent.click(screen.getByRole("button", { name: "Edit badge for Codex SDK" }));
    fireEvent.click(await screen.findByRole("switch", { name: "Show badge" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Badge text" }), {
      target: { value: "C" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(readAgentBadgePreferences().entries[builtin.id]).toEqual({
      label: "C",
      borderColor: "#8b5cf6",
      textColor: "theme",
    });
    expect(screen.getByTestId("agent-badge")).toHaveTextContent("C");

    fireEvent.click(screen.getByRole("button", { name: "Edit badge for Codex SDK" }));
    fireEvent.click(await screen.findByRole("switch", { name: "Show badge" }));
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(readAgentBadgePreferences().entries[builtin.id]).toBeUndefined());
    expect(screen.queryByTestId("agent-badge")).not.toBeInTheDocument();
  });

  it("shows custom Agent edit and delete controls and awaits both mutations", async () => {
    renderSettings();

    fireEvent.click(screen.getByRole("button", { name: "Edit Reviewer" }));
    const dialog = await screen.findByRole("dialog");
    const name = await within(dialog).findByRole("textbox", { name: "Name" });
    fireEvent.change(name, { target: { value: "Release reviewer" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Save" }));

    await waitFor(() =>
      expect(mocks.updateCustomAgent).toHaveBeenCalledWith(custom.id, {
        name: "Release reviewer",
        description: custom.description,
        instructions: customDetail.instructions,
        version: custom.version,
      }),
    );
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: "Delete Reviewer" }));
    const deleteDialog = await screen.findByRole("dialog");
    fireEvent.click(within(deleteDialog).getByRole("button", { name: "Delete Agent" }));

    await waitFor(() => expect(mocks.deleteCustomAgent).toHaveBeenCalledWith(custom.id));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });

  it("hides every badge globally without discarding saved entries", async () => {
    const entries = {
      [builtin.id]: { label: "C", borderColor: "#112233", textColor: "#ddeeff" },
      [custom.id]: { label: "R", borderColor: "#445566", textColor: "#ffffff" },
    };
    writeAgentBadgePreferences({
      version: 1,
      enabled: true,
      entries,
    });
    renderSettings();
    expect(screen.getAllByTestId("agent-badge")).toHaveLength(2);

    fireEvent.click(screen.getByRole("switch", { name: "Show Agent badges" }));

    await waitFor(() => expect(screen.queryByTestId("agent-badge")).not.toBeInTheDocument());
    const saved = JSON.parse(localStorage.getItem(AGENT_BADGE_STORAGE_KEY) ?? "null") as {
      enabled: boolean;
      entries: Record<string, unknown>;
    };
    expect(saved.enabled).toBe(false);
    expect(saved.entries).toEqual(entries);
  });

  it("keeps create open on an API error and closes only after a successful retry", async () => {
    mocks.createCustomAgent
      .mockRejectedValueOnce(new Error("Agent name already exists"))
      .mockResolvedValueOnce(customDetail);
    renderSettings();

    fireEvent.click(screen.getByRole("button", { name: "New" }));
    const dialog = await screen.findByTestId("create-agent-dialog");
    fireEvent.change(within(dialog).getByTestId("create-agent-name"), {
      target: { value: "Reviewer" },
    });
    fireEvent.pointerDown(within(dialog).getByTestId("agent-member-trigger"), {
      button: 0,
      pointerType: "mouse",
    });
    fireEvent.click(screen.getByTestId("agent-member-model"));
    fireEvent.click(await screen.findByTestId("agent-member-model-opus"));
    fireEvent.click(within(dialog).getByTestId("create-agent-submit"));

    expect(await within(dialog).findByRole("alert")).toHaveTextContent("Agent name already exists");
    expect(screen.getByTestId("create-agent-dialog")).toBeInTheDocument();

    fireEvent.click(within(dialog).getByTestId("create-agent-submit"));
    await waitFor(() => expect(mocks.createCustomAgent).toHaveBeenCalledTimes(2));
    await waitFor(() =>
      expect(screen.queryByTestId("create-agent-dialog")).not.toBeInTheDocument(),
    );
  });

  it("lists built-in Agents with View and Duplicate actions", () => {
    renderSettings();

    expect(screen.getByText("Polly")).toBeInTheDocument();
    expect(screen.getByText("Multi-agent coding · 3 members · read-only")).toBeInTheDocument();
    expect(screen.getByText("Debby")).toBeInTheDocument();
    expect(screen.getByText("Multi-agent debate · read-only")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "View Polly" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Duplicate Polly" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "View Debby" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Duplicate Debby" })).toBeEnabled();
  });

  it("keeps harness built-ins in the Built-in list with only their badge surface", () => {
    mocks.available = [builtinPolly, builtinClaudeNative, builtinAcp];
    renderSettings();

    const builtins = screen.getByRole("heading", { name: "Built-in" }).parentElement;
    expect(builtins).not.toBeNull();
    expect(within(builtins!).getByText("Polly")).toBeInTheDocument();
    for (const name of ["Claude Code", "Grok"]) {
      expect(within(builtins!).getByText(name)).toBeInTheDocument();
      expect(
        within(builtins!).getByRole("button", { name: `Edit badge for ${name}` }),
      ).toBeInTheDocument();
      expect(within(builtins!).queryByRole("button", { name: `View ${name}` })).toBeNull();
      expect(within(builtins!).queryByRole("button", { name: `Duplicate ${name}` })).toBeNull();
      expect(within(builtins!).queryByRole("button", { name: `Pin ${name}` })).toBeNull();
      expect(within(builtins!).queryByRole("button", { name: `Unpin ${name}` })).toBeNull();
    }
  });

  it("shows a built-in's roster, lead first, in a read-only dialog", async () => {
    renderSettings();

    fireEvent.click(screen.getByRole("button", { name: "View Polly" }));
    const dialog = await screen.findByRole("dialog");

    expect(within(dialog).getByText("Polly")).toBeInTheDocument();
    expect(within(dialog).getByText("Built-in · read-only")).toBeInTheDocument();
    const rows = within(dialog).getAllByTestId("builtin-member-row");
    expect(rows).toHaveLength(3);
    expect(within(rows[0]).getByText("polly")).toBeInTheDocument();
    expect(within(rows[0]).getByText("Lead")).toBeInTheDocument();
    expect(within(rows[0]).getByText("Claude SDK · opus")).toBeInTheDocument();
    expect(within(rows[1]).getByText("codex")).toBeInTheDocument();
    expect(within(rows[1]).getByText("Codex · Default · high")).toBeInTheDocument();
    expect(within(rows[1]).queryByText("Lead")).not.toBeInTheDocument();
    expect(
      within(dialog).getByText(
        "Built-in agents can't be changed. Duplicate Polly to get a copy you can edit.",
      ),
    ).toBeInTheDocument();
  });

  it("duplicates a built-in into an editable copy and opens the editor on it", async () => {
    const copy: CustomAgentDetail = { ...customDetail, id: "ca_polly_copy", name: "Polly" };
    mocks.duplicateBuiltinAgent.mockResolvedValue(copy);
    const { invalidate } = renderSettings();

    fireEvent.click(screen.getByRole("button", { name: "View Polly" }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Duplicate to edit" }));

    await waitFor(() =>
      expect(mocks.duplicateBuiltinAgent).toHaveBeenCalledWith("ag_builtin_polly"),
    );
    await waitFor(() => expect(invalidate).toHaveBeenCalledWith({ queryKey: ["custom-agents"] }));
    await waitFor(() => expect(mocks.getCustomAgent).toHaveBeenCalledWith("ca_polly_copy"));
    expect(await screen.findByTestId("agent-editor")).toBeInTheDocument();
  });

  it("reports a duplicate failure in the error line", async () => {
    mocks.duplicateBuiltinAgent.mockRejectedValue(new Error("Built-in agents are read-only"));
    renderSettings();

    fireEvent.click(screen.getByRole("button", { name: "Duplicate Polly" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Built-in agents are read-only");
  });

  it("summarises saved Agent rosters with one or many members", () => {
    mocks.catalog = [
      {
        ...custom,
        id: "ca_solo",
        name: "Solo",
        description: "One member",
        members: [{ ...leadMember, name: "Solo", reasoning_effort: "high" }],
      },
      {
        ...custom,
        id: "ca_crew",
        name: "Crew",
        description: "Three members",
        members: [
          { ...leadMember, name: "Crew" },
          { ...leadMember, name: "architect", lead: false },
          { ...leadMember, name: "reviewer", harness: "codex-native", model: null, lead: false },
        ],
      },
    ];
    renderSettings();

    expect(screen.getByText("Claude SDK · opus · high")).toBeInTheDocument();
    expect(screen.getByText("One member")).toBeInTheDocument();
    expect(screen.getByText("3 members · Crew (Lead), architect, reviewer")).toBeInTheDocument();
    expect(screen.getByText("Three members")).toBeInTheDocument();
  });

  it("pins and unpins Agents, materializing the Polly + Debby default", () => {
    renderSettings();

    // Nothing stored yet: the shipped pair reads as pinned.
    expect(screen.getByRole("button", { name: "Unpin Polly" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(screen.getByRole("button", { name: "Unpin Debby" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );

    fireEvent.click(screen.getByRole("button", { name: "Pin Reviewer" }));

    expect(JSON.parse(localStorage.getItem("omnigent:agent-pins")!)).toEqual({
      ids: ["ag_builtin_polly", "ag_builtin_debby", "ca_custom_reviewer"],
    });
    expect(screen.getByRole("button", { name: "Unpin Reviewer" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );

    fireEvent.click(screen.getByRole("button", { name: "Unpin Polly" }));

    expect(JSON.parse(localStorage.getItem("omnigent:agent-pins")!)).toEqual({
      ids: ["ag_builtin_debby", "ca_custom_reviewer"],
    });
    expect(screen.getByRole("button", { name: "Pin Polly" })).toHaveAttribute(
      "aria-pressed",
      "false",
    );
  });

  it("disables the pin button of unpinned rows at the cap", () => {
    mocks.catalog = [custom, { ...custom, id: "ca_second", name: "Second" }];
    renderSettings();

    fireEvent.click(screen.getByRole("button", { name: "Pin Reviewer" }));
    fireEvent.click(screen.getByRole("button", { name: "Pin Second" }));

    // The third pin filled the cap, so the fourth row's button is disabled.
    expect(screen.getByRole("button", { name: "Pin Second" })).toBeDisabled();
    expect(JSON.parse(localStorage.getItem("omnigent:agent-pins")!)).toEqual({
      ids: ["ag_builtin_polly", "ag_builtin_debby", "ca_custom_reviewer"],
    });
    // Pinned rows stay toggleable so a slot can be swapped.
    expect(screen.getByRole("button", { name: "Unpin Polly" })).toBeEnabled();
  });

  it("removes a deleted saved Agent from the pins", async () => {
    renderSettings();
    fireEvent.click(screen.getByRole("button", { name: "Pin Reviewer" }));
    expect(JSON.parse(localStorage.getItem("omnigent:agent-pins")!).ids).toContain(
      "ca_custom_reviewer",
    );

    fireEvent.click(screen.getByRole("button", { name: "Delete Reviewer" }));
    const deleteDialog = await screen.findByRole("dialog");
    fireEvent.click(within(deleteDialog).getByRole("button", { name: "Delete Agent" }));
    await waitFor(() => expect(mocks.deleteCustomAgent).toHaveBeenCalledWith(custom.id));

    expect(JSON.parse(localStorage.getItem("omnigent:agent-pins")!)).toEqual({
      ids: ["ag_builtin_polly", "ag_builtin_debby"],
    });
  });

  it("disables the Pin buttons while the available list has no data yet", () => {
    mocks.available = undefined;
    renderSettings();

    expect(screen.getByRole("button", { name: "Pin Reviewer" })).toBeDisabled();
  });

  it.each([
    [
      "the catalog is pending",
      { catalogLoading: true, catalog: undefined },
      ["Pin Debby", "Unpin Polly"],
    ],
    ["the catalog failed", { catalogError: new Error("offline") }, ["Pin Debby", "Unpin Polly"]],
    [
      "the built-in list is pending",
      { availableLoading: true, available: undefined },
      ["Pin Reviewer"],
    ],
    [
      "the built-in list failed",
      { availableError: new Error("offline") },
      ["Pin Debby", "Unpin Polly"],
    ],
  ])("disables the Pin buttons while %s", (_label, state, disabledPins) => {
    localStorage.setItem("omnigent:agent-pins", JSON.stringify({ ids: ["ag_builtin_polly"] }));
    Object.assign(mocks, state);
    renderSettings();

    for (const name of disabledPins) {
      expect(screen.getByRole("button", { name })).toBeDisabled();
    }
  });
});
