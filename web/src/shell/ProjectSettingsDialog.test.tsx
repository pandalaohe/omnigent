import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { ProjectSettingsDialog } from "./ProjectSettingsDialog";
import {
  createProject,
  deleteProjectEntry,
  getProject,
  getProjectCollaboration,
  getProjectHostRoots,
  listProjectEntries,
  putProjectEntry,
  updateProjectConfig,
} from "@/lib/projectsApi";
import { TooltipProvider } from "@/components/ui/tooltip";

vi.mock("@/lib/projectsApi", () => ({
  createProject: vi.fn(),
  deleteProjectEntry: vi.fn(),
  getProject: vi.fn(),
  getProjectCollaboration: vi.fn(),
  getProjectHostRoots: vi.fn(),
  listProjectEntries: vi.fn(),
  putProjectEntry: vi.fn(),
  putProjectRepository: vi.fn(),
  deleteProjectRepository: vi.fn(),
  putProjectHostBinding: vi.fn(),
  deleteProjectHostBinding: vi.fn(),
  verifyProjectHostBinding: vi.fn(),
  updateProjectConfig: vi.fn(),
}));
// Hoisted so the vi.mock factory below can reference it; per-test overrides
// let cases control the host list, the agent catalog, and the server info
// (the defaults are set in beforeEach).
const {
  hostsMock,
  availableAgentsMock,
  hostModelOptionsMock,
  serverInfoMock,
  workspacePickerPropsMock,
  listCatalogsMock,
  syncCatalogsMock,
} = vi.hoisted(() => ({
  hostsMock: vi.fn(),
  availableAgentsMock: vi.fn(),
  hostModelOptionsMock: vi.fn(),
  serverInfoMock: vi.fn(),
  workspacePickerPropsMock: vi.fn(),
  listCatalogsMock: vi.fn(),
  syncCatalogsMock: vi.fn(),
}));
vi.mock("@/lib/callingDefaultsApi", () => ({
  listCallingDefaultCatalogs: () => listCatalogsMock(),
  syncCallingDefaults: (options: unknown) => syncCatalogsMock(options),
}));
const LAPTOP = {
  host_id: "h1",
  name: "Laptop",
  owner: "me",
  status: "online",
  default_workspace: "/Users/me/Projects",
};
const DESKTOP = {
  host_id: "h2",
  name: "Desktop",
  owner: "me",
  status: "online",
  default_workspace: null,
};
const SERVER = {
  host_id: "h3",
  name: "Build server",
  owner: "me",
  status: "offline",
  default_workspace: null,
};
vi.mock("@/hooks/useHosts", () => ({
  useHosts: () => hostsMock(),
  useHostModelOptions: hostModelOptionsMock,
}));
vi.mock("@/hooks/useAvailableAgents", () => ({
  useAvailableAgents: availableAgentsMock,
  // The reused agent picker prefetches details on open; no-op in the dialog test.
  prefetchAvailableAgentDetails: vi.fn(),
}));

function pickerAgent(overrides: Record<string, unknown> = {}) {
  return {
    id: "ag_1",
    name: "hello",
    display_name: "Hello",
    description: null,
    harness: "claude-sdk",
    skills: [],
    ...overrides,
  };
}
vi.mock("@/lib/CapabilitiesContext", () => ({
  useServerInfo: serverInfoMock,
}));
// The filesystem browser owns its own data-fetching; stub its explicit commit
// action so this suite can drive the shared dialog without filesystem plumbing.
vi.mock("./WorkspacePicker", () => ({
  isNavigablePath: (p: string) => p.startsWith("/"),
  HostWorkspacePicker: (props: { onSelect: (p: string) => void }) => {
    workspacePickerPropsMock(props);
    return (
      <div data-testid="mock-workspace-picker">
        <button type="button" onClick={() => props.onSelect("/picked/dir")}>
          pick dir
        </button>
      </div>
    );
  },
}));
// The Code section owns its own data-fetching; stub it so this suite can
// assert the dialog mounts it without wiring the whole code API surface.
vi.mock("./ProjectCodeSection", () => ({
  ProjectCodeSection: ({ projectId }: { projectId: string }) => (
    <div data-testid="project-code-section" data-project-id={projectId} />
  ),
}));

const getProjectMock = vi.mocked(getProject);
const getCollaborationMock = vi.mocked(getProjectCollaboration);
const getHostRootsMock = vi.mocked(getProjectHostRoots);
const updateMock = vi.mocked(updateProjectConfig);
const createMock = vi.mocked(createProject);
const listEntriesMock = vi.mocked(listProjectEntries);
const putEntryMock = vi.mocked(putProjectEntry);
const deleteEntryMock = vi.mocked(deleteProjectEntry);

function entry(hostId: string, workspace: string) {
  return { host_id: hostId, workspace, updated_at: null };
}

async function pickOption(triggerTestId: string, optionName: string) {
  fireEvent.click(screen.getByTestId(triggerTestId));
  fireEvent.click(await screen.findByRole("option", { name: optionName }));
}

function catalogRow(hostId: string, harness: string, models: Record<string, unknown>[]) {
  return { host_id: hostId, harness, models, fetched_at: 1_700_000_000, stale: false, error: null };
}

/** Force the max-md breakpoint on, so the dialog renders its accordion. */
function useMobileViewport(): () => void {
  const original = window.matchMedia;
  window.matchMedia = ((query: string) => ({
    matches: query === "(max-width: 767.98px)",
    media: query,
    onchange: null,
    addListener: () => {},
    removeListener: () => {},
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => false,
  })) as typeof window.matchMedia;
  return () => {
    window.matchMedia = original;
  };
}

function renderDialog(projectId: string | null = "p_1", onOpenChangeSpy?: (open: boolean) => void) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const onOpenChange = onOpenChangeSpy ?? vi.fn();
  const view = (open: boolean, pid: string | null = projectId) => (
    <QueryClientProvider client={client}>
      <TooltipProvider>
        <ProjectSettingsDialog
          open={open}
          onOpenChange={onOpenChange}
          projectId={pid}
          projectName="Work"
        />
      </TooltipProvider>
    </QueryClientProvider>
  );
  const result = render(view(true));
  return {
    ...result,
    client,
    onOpenChange,
    rerenderOpen: (open: boolean) => result.rerender(view(open)),
    rerenderProject: (pid: string | null) => result.rerender(view(true, pid)),
  };
}

beforeEach(() => {
  getProjectMock.mockReset();
  getCollaborationMock.mockReset();
  getCollaborationMock.mockResolvedValue({ repositories: [], bindings: [], problems: [] });
  getHostRootsMock.mockReset();
  updateMock.mockReset();
  createMock.mockReset();
  listEntriesMock.mockReset();
  putEntryMock.mockReset();
  deleteEntryMock.mockReset();
  hostsMock.mockReset();
  availableAgentsMock.mockReset();
  hostModelOptionsMock.mockReset();
  serverInfoMock.mockReset();
  workspacePickerPropsMock.mockReset();
  listCatalogsMock.mockReset();
  syncCatalogsMock.mockReset();
  hostsMock.mockReturnValue({ data: [LAPTOP] });
  availableAgentsMock.mockReturnValue({ data: [pickerAgent()] });
  hostModelOptionsMock.mockReturnValue({ data: [] });
  listCatalogsMock.mockResolvedValue([]);
  syncCatalogsMock.mockResolvedValue([]);
  serverInfoMock.mockReturnValue({
    managed_sandboxes_enabled: false,
    sandbox_provider: null,
    features: {},
  });
  listEntriesMock.mockResolvedValue([]);
  putEntryMock.mockImplementation(async (_id, hostId, workspace) => entry(hostId, workspace));
  deleteEntryMock.mockResolvedValue(undefined);
  updateMock.mockResolvedValue({ id: "p_1", name: "Work", config: {} });
  getHostRootsMock.mockResolvedValue({
    roots: [],
    default_host_id: null,
    default_host_reason: "none",
  });
});

afterEach(cleanup);

describe("ProjectSettingsDialog", () => {
  it("seeds fields from the project's stored config", async () => {
    getProjectMock.mockResolvedValue({
      id: "p_1",
      name: "Work",
      config: { use_worktree: true },
    });
    renderDialog();
    // use_worktree:true was stored → toggle seeds ON once the fetch settles.
    await waitFor(() =>
      expect(screen.getByTestId("project-settings-worktree")).toHaveAttribute(
        "data-state",
        "checked",
      ),
    );
    // No entries and no config workspace → no directory rows seeded; the empty
    // state offers the Add host control.
    expect(screen.getByTestId("project-settings-directories-empty")).toBeInTheDocument();
  });

  it("saves only the fields that are set (unset slots omitted)", async () => {
    getProjectMock.mockResolvedValue({ id: "p_1", name: "Work", config: {} });
    renderDialog();
    // Wait for the config fetch to settle (Save enabled) so the seeding effect
    // has run before we interact — otherwise the seed would clobber our change.
    await waitFor(() =>
      expect((screen.getByTestId("project-settings-save") as HTMLButtonElement).disabled).toBe(
        false,
      ),
    );

    // Turn the worktree default ON — the only field touched.
    fireEvent.click(screen.getByTestId("project-settings-worktree"));
    fireEvent.click(screen.getByTestId("project-settings-save"));

    await waitFor(() => expect(updateMock).toHaveBeenCalled());
    expect(updateMock).toHaveBeenCalledWith("p_1", { use_worktree: true });
  });

  it("preserves the project icon when saving settings", async () => {
    getProjectMock.mockResolvedValue({
      id: "p_1",
      name: "Work",
      config: { icon: "🔥", use_worktree: true },
    });
    renderDialog();
    await waitFor(() =>
      expect(screen.getByTestId("project-settings-worktree")).toHaveAttribute(
        "data-state",
        "checked",
      ),
    );

    fireEvent.click(screen.getByTestId("project-settings-save"));

    await waitFor(() =>
      expect(updateMock).toHaveBeenCalledWith("p_1", { icon: "🔥", use_worktree: true }),
    );
  });

  it("stores nothing for the worktree toggle when left at its default (OFF)", async () => {
    getProjectMock.mockResolvedValue({ id: "p_1", name: "Work", config: {} });
    renderDialog();
    await waitFor(() =>
      expect((screen.getByTestId("project-settings-save") as HTMLButtonElement).disabled).toBe(
        false,
      ),
    );
    // Leave the toggle OFF (default) → config clears to {} (use_worktree absent).
    fireEvent.click(screen.getByTestId("project-settings-save"));
    await waitFor(() => expect(updateMock).toHaveBeenCalledWith("p_1", {}));
  });

  it("shows the base-branch field only when the worktree default is on, and saves it", async () => {
    getProjectMock.mockResolvedValue({ id: "p_1", name: "Work", config: {} });
    renderDialog();
    await waitFor(() =>
      expect((screen.getByTestId("project-settings-save") as HTMLButtonElement).disabled).toBe(
        false,
      ),
    );
    // Base branch is hidden while the worktree default is OFF (nothing to fork).
    expect(screen.queryByTestId("project-settings-base-branch")).not.toBeInTheDocument();

    // Turning the worktree default ON reveals the base-branch field.
    fireEvent.click(screen.getByTestId("project-settings-worktree"));
    const input = screen.getByTestId("project-settings-base-branch");
    fireEvent.change(input, { target: { value: "  main  " } });
    fireEvent.click(screen.getByTestId("project-settings-save"));

    // Trimmed and stored alongside the worktree default.
    await waitFor(() =>
      expect(updateMock).toHaveBeenCalledWith("p_1", { use_worktree: true, base_branch: "main" }),
    );
  });

  it("drops the base branch when the worktree default is off", async () => {
    // A base branch stored from an earlier ON state must not linger as an
    // invisible default once the worktree toggle is turned back off.
    getProjectMock.mockResolvedValue({
      id: "p_1",
      name: "Work",
      config: { use_worktree: true, base_branch: "develop" },
    });
    renderDialog();
    // Seeded ON → the base-branch field shows its stored value.
    await waitFor(() =>
      expect(screen.getByTestId("project-settings-base-branch")).toHaveValue("develop"),
    );
    // Turn the worktree default OFF → base branch drops, config clears to {}.
    fireEvent.click(screen.getByTestId("project-settings-worktree"));
    fireEvent.click(screen.getByTestId("project-settings-save"));
    await waitFor(() => expect(updateMock).toHaveBeenCalledWith("p_1", {}));
  });

  it("hides the sandbox option when managed sandboxes are disabled", async () => {
    getProjectMock.mockResolvedValue({ id: "p_1", name: "Work", config: {} });
    renderDialog();
    await waitFor(() =>
      expect((screen.getByTestId("project-settings-save") as HTMLButtonElement).disabled).toBe(
        false,
      ),
    );
    // The online host is offered, but no "Sandbox" option (managed sandboxes off).
    expect(screen.getAllByText("Laptop").length).toBeGreaterThan(0);
    expect(screen.queryByText(/Sandbox/)).not.toBeInTheDocument();
  });

  it("promotes a label-only folder (id=null) on save via createProject", async () => {
    createMock.mockResolvedValue({ id: "p_new", name: "Work" });
    renderDialog(null);
    // No fetch for a label-only folder (nothing to read).
    expect(getProjectMock).not.toHaveBeenCalled();

    fireEvent.click(screen.getByTestId("project-settings-worktree"));
    fireEvent.click(screen.getByTestId("project-settings-save"));

    await waitFor(() => expect(createMock).toHaveBeenCalledWith("Work"));
    expect(updateMock).toHaveBeenCalledWith("p_new", { use_worktree: true });
  });

  it("persists host, workspace, and agent from a seeded config on save", async () => {
    getProjectMock.mockResolvedValue({
      id: "p_1",
      name: "Work",
      config: { host_id: "h1", workspace: "/repo", agent_id: "ag_1" },
    });
    renderDialog();
    await waitFor(() =>
      expect((screen.getByTestId("project-settings-save") as HTMLButtonElement).disabled).toBe(
        false,
      ),
    );
    // The legacy config workspace shows as the row's folder, and Save mirrors
    // it back — without writing an entry (folders belong to the Code tab).
    expect(screen.getByTestId("project-settings-entry-h1")).toHaveTextContent("/repo");
    fireEvent.click(screen.getByTestId("project-settings-save"));
    await waitFor(() =>
      expect(updateMock).toHaveBeenCalledWith("p_1", {
        host_id: "h1",
        workspace: "/repo",
        agent_id: "ag_1",
      }),
    );
    expect(putEntryMock).not.toHaveBeenCalled();
    expect(deleteEntryMock).not.toHaveBeenCalled();
  });

  it("loads one project-directory row per host from the entries API", async () => {
    hostsMock.mockReturnValue({ data: [LAPTOP, DESKTOP] });
    listEntriesMock.mockResolvedValue([entry("h1", "/repo/one"), entry("h2", "/repo/two")]);
    getProjectMock.mockResolvedValue({ id: "p_1", name: "Work", config: {} });
    renderDialog();

    await waitFor(() =>
      expect(screen.getByTestId("project-settings-entry-h1")).toHaveTextContent("Laptop"),
    );
    expect(screen.getByTestId("project-settings-entry-h1")).toHaveTextContent("/repo/one");
    expect(screen.getByTestId("project-settings-entry-h2")).toHaveTextContent("Desktop");
    expect(screen.getByTestId("project-settings-entry-h2")).toHaveTextContent("/repo/two");
    // Both hosts already have a row → nothing left to add.
    expect(screen.queryByTestId("project-settings-add-host")).not.toBeInTheDocument();
  });

  it("keeps a stored config workspace when the default host has no folder (scenario 15)", async () => {
    getProjectMock.mockResolvedValue({
      id: "p_1",
      name: "Work",
      config: { host_id: "h1", workspace: "/legacy/repo" },
    });
    renderDialog();

    // The legacy config workspace still shows as a row on the default host.
    await waitFor(() =>
      expect(screen.getByTestId("project-settings-entry-h1")).toHaveTextContent("/legacy/repo"),
    );
    fireEvent.click(screen.getByTestId("project-settings-save"));
    // No entry exists, so Save keeps the stored workspace instead of deleting
    // it, and writes no entry (the Code tab owns folders).
    await waitFor(() =>
      expect(updateMock).toHaveBeenCalledWith("p_1", {
        host_id: "h1",
        workspace: "/legacy/repo",
      }),
    );
    expect(putEntryMock).not.toHaveBeenCalled();
    expect(deleteEntryMock).not.toHaveBeenCalled();
  });

  it("stops at the first directory error and writes no config for a label-only folder", async () => {
    hostsMock.mockReturnValue({ data: [LAPTOP, SERVER] });
    createMock.mockResolvedValue({ id: "p_new", name: "Work" });
    const onOpenChange = vi.fn();
    // The first PUT fails (offline host) — no config must be written.
    putEntryMock.mockRejectedValue(new Error("host is offline"));
    renderDialog(null, onOpenChange);

    fireEvent.click(screen.getByTestId("project-settings-add-host"));
    fireEvent.click(screen.getByRole("option", { name: "Build server" }));
    fireEvent.change(screen.getByTestId("project-settings-entry-path-h3"), {
      target: { value: "/srv/repo" },
    });
    fireEvent.click(screen.getByTestId("project-settings-save"));

    await waitFor(() =>
      expect(screen.getByTestId("project-settings-entry-error-h3")).toHaveTextContent(
        "host is offline",
      ),
    );
    expect(updateMock).not.toHaveBeenCalled();
    expect(onOpenChange).not.toHaveBeenCalledWith(false);
  });

  it("promotes a label-only folder first, then PUTs the entry with the new id (scenario 26)", async () => {
    hostsMock.mockReturnValue({ data: [LAPTOP, SERVER] });
    createMock.mockResolvedValue({ id: "p_new", name: "Work" });
    renderDialog(null);
    // No fetch for a label-only folder (nothing to read).
    expect(getProjectMock).not.toHaveBeenCalled();

    // Add the offline host and type its directory — no first-class project id
    // exists yet, so the entry PUT can only run after the create.
    fireEvent.click(screen.getByTestId("project-settings-add-host"));
    fireEvent.click(screen.getByRole("option", { name: "Build server" }));
    fireEvent.change(screen.getByTestId("project-settings-entry-path-h3"), {
      target: { value: "/srv/work" },
    });
    fireEvent.click(screen.getByTestId("project-settings-save"));

    await waitFor(() => expect(createMock).toHaveBeenCalledWith("Work"));
    expect(putEntryMock).toHaveBeenCalledWith("p_new", "h3", "/srv/work");
    expect(updateMock).toHaveBeenCalledWith("p_new", {});
    const order = (mock: { mock: { invocationCallOrder: number[] } }) =>
      mock.mock.invocationCallOrder[0];
    expect(order(createMock)).toBeLessThan(order(putEntryMock));
    expect(order(putEntryMock)).toBeLessThan(order(updateMock));
  });

  it("keeps a failed post-bind warning after a label-only folder is promoted", async () => {
    hostsMock.mockReturnValue({ data: [LAPTOP, SERVER] });
    createMock.mockResolvedValue({ id: "p_new", name: "Work" });
    getProjectMock.mockResolvedValue({ id: "p_new", name: "Work", config: {} });
    listEntriesMock.mockResolvedValue([entry("h3", "/srv/work")]);
    putEntryMock.mockResolvedValue({
      ...entry("h3", "/srv/work"),
      post_bind: { status: "failed", error: "boom", exit_code: 1, output: null },
    });
    const { rerenderProject } = renderDialog(null);

    fireEvent.click(screen.getByTestId("project-settings-add-host"));
    fireEvent.click(screen.getByRole("option", { name: "Build server" }));
    fireEvent.change(screen.getByTestId("project-settings-entry-path-h3"), {
      target: { value: "/srv/work" },
    });
    fireEvent.click(screen.getByTestId("project-settings-save"));

    const warning = "Directory saved; post-bind command failed: boom (exit code 1)";
    await waitFor(() =>
      expect(screen.getByTestId("project-settings-entry-post-bind-h3")).toHaveTextContent(warning),
    );

    // The parent re-renders with the promoted id, so `labelOnly` turns false.
    // The retained warning must survive, and no Directory field may reappear.
    rerenderProject("p_new");
    await waitFor(() =>
      expect(screen.getByTestId("project-settings-entry-post-bind-h3")).toHaveTextContent(warning),
    );
    expect(screen.queryByTestId("project-settings-entry-path-h3")).not.toBeInTheDocument();
    expect(screen.queryByTestId("project-settings-entry-browse-h3")).not.toBeInTheDocument();
  });

  it("opens a row's directory dialog and commits the confirmed path (label-only folder)", async () => {
    createMock.mockResolvedValue({ id: "p_new", name: "Work" });
    renderDialog(null);

    // Add the online host, make it the default, then open the shared dialog
    // against its row; confirming updates the trigger label.
    fireEvent.click(screen.getByTestId("project-settings-add-host"));
    fireEvent.click(await screen.findByRole("option", { name: "Laptop" }));
    await pickOption("project-settings-host", "Laptop");
    fireEvent.click(screen.getByTestId("project-settings-entry-browse-h1"));
    expect(screen.getByTestId("mock-workspace-picker")).toBeInTheDocument();
    expect(workspacePickerPropsMock).toHaveBeenLastCalledWith(
      expect.objectContaining({ hostId: "h1" }),
    );
    fireEvent.click(screen.getByText("pick dir"));
    expect(screen.getByTestId("project-settings-entry-browse-h1")).toHaveTextContent("/picked/dir");

    expect(screen.queryByTestId("mock-workspace-picker")).not.toBeInTheDocument();

    fireEvent.click(screen.getByTestId("project-settings-save"));
    await waitFor(() => expect(putEntryMock).toHaveBeenCalledWith("p_new", "h1", "/picked/dir"));
    expect(updateMock).toHaveBeenCalledWith("p_new", { host_id: "h1", workspace: "/picked/dir" });
  });

  it("blocks Save (does not clear defaults) when the config load fails", async () => {
    // A transient GET failure must NOT be read as "no config" — otherwise
    // saving the blank draft would send `{}` and wipe the stored defaults.
    getProjectMock.mockRejectedValue(new Error("500 Server Error"));
    renderDialog();

    // The load-error notice shows and Save stays disabled.
    await waitFor(() =>
      expect(screen.getByTestId("project-settings-load-error")).toBeInTheDocument(),
    );
    expect((screen.getByTestId("project-settings-save") as HTMLButtonElement).disabled).toBe(true);

    // Even if a submit is forced, onSubmit bails — no clearing PATCH is sent.
    fireEvent.submit(document.getElementById("project-settings-defaults-form")!);
    expect(updateMock).not.toHaveBeenCalled();
  });

  it("offers the same agent set as the composer picker (hidden agents excluded)", async () => {
    // Filter parity with the new-session composer (selectableSessionAgents):
    // if this picker offered an agent the composer hides, a project could pin
    // a default the composer then can't show — the silent-substitution setup.
    availableAgentsMock.mockReturnValue({
      data: [
        pickerAgent(),
        pickerAgent({ id: "ag_nessie", name: "nessie", display_name: "Nessie" }),
      ],
    });
    getProjectMock.mockResolvedValue({ id: "p_1", name: "Work", config: {} });
    renderDialog();
    await waitFor(() =>
      expect((screen.getByTestId("project-settings-save") as HTMLButtonElement).disabled).toBe(
        false,
      ),
    );

    // Open the agent picker dropdown (Radix opens on pointerdown), then the
    // custom-agent "Other..." submenu where composed agents are listed.
    fireEvent.pointerDown(screen.getByTestId("new-chat-landing-agent-select"), { button: 0 });
    fireEvent.click(screen.getByTestId("new-chat-landing-custom-agents"));
    expect(screen.getByTestId("new-chat-landing-agent-ag_1")).toBeInTheDocument();
    expect(screen.queryByTestId("new-chat-landing-agent-ag_nessie")).not.toBeInTheDocument();
    // And the stored default agent is pinned into discovery, so a
    // session-scoped default that the bounded scan misses still resolves here.
    expect(
      availableAgentsMock.mock.calls.some(
        ([opts]) => (opts as { pinnedAgentIds?: string[] } | undefined)?.pinnedAgentIds != null,
      ),
    ).toBe(true);
  });

  // A model default belongs only to a native harness that takes a model
  // override (Claude Code / Codex). These pin the control's visibility, its
  // round-trip through save, and the data-safety edges around it.
  const claudeAgent = () =>
    pickerAgent({
      id: "ag_claude",
      name: "claude-native-ui",
      display_name: "Claude Code",
      harness: "claude-native",
    });

  it("offers a model default only when the default agent has a model choice", async () => {
    availableAgentsMock.mockReturnValue({ data: [pickerAgent(), claudeAgent()] });
    getProjectMock.mockResolvedValue({
      id: "p_1",
      name: "Work",
      config: { agent_id: "ag_claude" },
    });
    renderDialog();
    await waitFor(() => expect(screen.getByTestId("project-settings-model")).toBeInTheDocument());

    // Switch the default to a plain bundle agent (under "Other...") → the
    // model field goes away.
    fireEvent.pointerDown(screen.getByTestId("new-chat-landing-agent-select"), { button: 0 });
    fireEvent.click(screen.getByTestId("new-chat-landing-custom-agents"));
    fireEvent.click(screen.getByTestId("new-chat-landing-agent-ag_1"));
    await waitFor(() =>
      expect(screen.queryByTestId("project-settings-model")).not.toBeInTheDocument(),
    );
  });

  it("round-trips a stored model default on save", async () => {
    availableAgentsMock.mockReturnValue({ data: [claudeAgent()] });
    const catalogRows = [catalogRow("h1", "claude-native", [{ id: "opus", displayName: "Opus" }])];
    listCatalogsMock.mockResolvedValue(catalogRows);
    syncCatalogsMock.mockResolvedValue(catalogRows);
    getProjectMock.mockResolvedValue({
      id: "p_1",
      name: "Work",
      config: { agent_id: "ag_claude", model: "opus" },
    });
    renderDialog();
    await waitFor(() => expect(screen.getByTestId("project-settings-model")).toBeInTheDocument());
    // The stored model is in the hosts' offer and seeds the control.
    await waitFor(() =>
      expect(screen.getByTestId("project-settings-model")).toHaveTextContent("Opus"),
    );

    // Save untouched — the model rides back out unchanged.
    fireEvent.click(screen.getByTestId("project-settings-save"));
    await waitFor(() =>
      expect(updateMock).toHaveBeenCalledWith("p_1", { agent_id: "ag_claude", model: "opus" }),
    );
  });

  it("drops a stale model default when the agent has no model choice", async () => {
    // A model stored while Claude Code was the default must not linger as an
    // invisible key after the default agent changes to one without a model
    // override (nothing would consume it, and the field can't clear it).
    getProjectMock.mockResolvedValue({
      id: "p_1",
      name: "Work",
      config: { agent_id: "ag_1", model: "opus" },
    });
    renderDialog();
    await waitFor(() =>
      expect((screen.getByTestId("project-settings-save") as HTMLButtonElement).disabled).toBe(
        false,
      ),
    );
    expect(screen.queryByTestId("project-settings-model")).not.toBeInTheDocument();
    fireEvent.click(screen.getByTestId("project-settings-save"));
    await waitFor(() => expect(updateMock).toHaveBeenCalledWith("p_1", { agent_id: "ag_1" }));
  });

  it("preserves a stored model when the default agent hasn't resolved from discovery", async () => {
    // While agent discovery is loading/failing, the stored agent's model
    // capability is unknowable — an unrelated save in that window must not
    // silently delete a valid stored default.
    availableAgentsMock.mockReturnValue({ data: [] });
    getProjectMock.mockResolvedValue({
      id: "p_1",
      name: "Work",
      config: { agent_id: "ag_claude", model: "opus" },
    });
    renderDialog();
    await waitFor(() =>
      expect((screen.getByTestId("project-settings-save") as HTMLButtonElement).disabled).toBe(
        false,
      ),
    );
    fireEvent.click(screen.getByTestId("project-settings-save"));
    await waitFor(() =>
      expect(updateMock).toHaveBeenCalledWith("p_1", { agent_id: "ag_claude", model: "opus" }),
    );
  });

  // The All hosts model field lists from the cached per-host catalogs (the
  // same rows the server's create-time offered check reads), refreshed by one
  // full sync each time the dialog opens.
  it("syncs all catalogs on open and lists the model offer from the synced rows", async () => {
    availableAgentsMock.mockReturnValue({ data: [claudeAgent()] });
    getProjectMock.mockResolvedValue({
      id: "p_1",
      name: "Work",
      config: { agent_id: "ag_claude" },
    });
    // The cached list is older (opus only); the sync widens the offer. The
    // list resolves after the sync and must not overwrite its rows.
    let resolveList!: (rows: unknown[]) => void;
    listCatalogsMock.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveList = resolve;
        }),
    );
    syncCatalogsMock.mockResolvedValue([
      catalogRow("h1", "claude-native", [
        { id: "opus", displayName: "Opus" },
        { id: "sonnet", displayName: "Sonnet" },
      ]),
      catalogRow("h2", "claude-native", [
        { id: "opus", displayName: "Opus" },
        { id: "sonnet", displayName: "Sonnet" },
      ]),
    ]);
    renderDialog();
    await waitFor(() => expect(syncCatalogsMock).toHaveBeenCalledTimes(1));
    expect(syncCatalogsMock).toHaveBeenCalledWith(undefined);

    // The synced rows are applied once the field's hint leaves the empty /
    // refreshing states.
    await waitFor(() =>
      expect(
        screen.getByText("Default model for new sessions with this agent"),
      ).toBeInTheDocument(),
    );
    resolveList([catalogRow("h1", "claude-native", [{ id: "opus", displayName: "Opus" }])]);

    fireEvent.click(screen.getByTestId("project-settings-model"));
    await waitFor(() =>
      expect(screen.getAllByRole("option").map((o) => o.textContent)).toEqual([
        "No default",
        "Opus",
        "Sonnet",
      ]),
    );
  });

  it("offers only the models every host's catalog lists, skipping errored rows", async () => {
    availableAgentsMock.mockReturnValue({ data: [claudeAgent()] });
    getProjectMock.mockResolvedValue({
      id: "p_1",
      name: "Work",
      config: { agent_id: "ag_claude" },
    });
    const catalogRows = [
      catalogRow("h1", "claude-native", [
        { id: "opus", displayName: "Opus" },
        { id: "sonnet", displayName: "Sonnet" },
      ]),
      catalogRow("h2", "claude-native", [{ id: "opus", displayName: "Opus" }]),
      // An errored row is stale: it neither narrows nor widens the offer.
      {
        ...catalogRow("h3", "claude-native", [{ id: "haiku" }]),
        stale: true,
        error: "unsupported",
      },
    ];
    listCatalogsMock.mockResolvedValue(catalogRows);
    syncCatalogsMock.mockResolvedValue(catalogRows);
    renderDialog();
    await waitFor(() =>
      expect(
        screen.getByText("Default model for new sessions with this agent"),
      ).toBeInTheDocument(),
    );

    fireEvent.click(screen.getByTestId("project-settings-model"));
    await waitFor(() =>
      expect(screen.getAllByRole("option").map((o) => o.textContent)).toEqual([
        "No default",
        "Opus",
      ]),
    );
  });

  it("does not re-sync when the dialog reopens inside the freshness window", async () => {
    getProjectMock.mockResolvedValue({ id: "p_1", name: "Work", config: {} });
    const { rerenderOpen } = renderDialog();
    await waitFor(() => expect(syncCatalogsMock).toHaveBeenCalledTimes(1));
    // Let the resolved sync land in the shared query cache before reopening.
    await act(async () => {});

    rerenderOpen(false);
    rerenderOpen(true);
    // The reopen re-lists the cached rows but skips the still-fresh sync.
    await waitFor(() => expect(listCatalogsMock).toHaveBeenCalledTimes(2));
    expect(syncCatalogsMock).toHaveBeenCalledTimes(1);
  });

  it("marks a stored model the offer lacks as not offered by every host", async () => {
    availableAgentsMock.mockReturnValue({ data: [claudeAgent()] });
    const catalogRows = [
      catalogRow("h1", "claude-native", [{ id: "sonnet", displayName: "Sonnet" }]),
    ];
    listCatalogsMock.mockResolvedValue(catalogRows);
    syncCatalogsMock.mockResolvedValue(catalogRows);
    getProjectMock.mockResolvedValue({
      id: "p_1",
      name: "Work",
      config: { agent_id: "ag_claude", model: "opus" },
    });
    renderDialog();
    await waitFor(() =>
      expect(
        screen.getByText("Not offered by every host — pick another model or set it per host below"),
      ).toBeInTheDocument(),
    );
    expect(screen.getByTestId("project-settings-model")).toHaveTextContent(
      "opus (not offered by every host)",
    );

    // The stored value stays selectable so an unrelated save can't drop it.
    fireEvent.click(screen.getByTestId("project-settings-save"));
    await waitFor(() =>
      expect(updateMock).toHaveBeenCalledWith("p_1", { agent_id: "ag_claude", model: "opus" }),
    );
  });

  it("does not flag a stored model when no host has a catalog for the harness", async () => {
    availableAgentsMock.mockReturnValue({ data: [claudeAgent()] });
    listCatalogsMock.mockResolvedValue([]);
    syncCatalogsMock.mockResolvedValue([]);
    getProjectMock.mockResolvedValue({
      id: "p_1",
      name: "Work",
      config: { agent_id: "ag_claude", model: "opus" },
    });
    renderDialog();
    await waitFor(() =>
      expect(
        screen.getByText("Default model for new sessions with this agent"),
      ).toBeInTheDocument(),
    );
    expect(screen.getByTestId("project-settings-model")).toHaveTextContent("opus");
    expect(screen.getByTestId("project-settings-model")).not.toHaveTextContent("not offered");
  });

  it("does not flag a stored model every host accepts by its wire model", async () => {
    availableAgentsMock.mockReturnValue({ data: [claudeAgent()] });
    const catalogRows = [
      catalogRow("h1", "claude-native", [
        { id: "opus", model: "claude-opus-5-5", displayName: "Opus" },
      ]),
    ];
    listCatalogsMock.mockResolvedValue(catalogRows);
    syncCatalogsMock.mockResolvedValue(catalogRows);
    getProjectMock.mockResolvedValue({
      id: "p_1",
      name: "Work",
      config: { agent_id: "ag_claude", model: "claude-opus-5-5" },
    });
    renderDialog();
    await waitFor(() =>
      expect(
        screen.getByText("Default model for new sessions with this agent"),
      ).toBeInTheDocument(),
    );
    expect(screen.getByTestId("project-settings-model")).toHaveTextContent("claude-opus-5-5");
    expect(screen.getByTestId("project-settings-model")).not.toHaveTextContent("not offered");
  });

  it("drops a list answer from an earlier opening after a reopen", async () => {
    availableAgentsMock.mockReturnValue({ data: [claudeAgent()] });
    getProjectMock.mockResolvedValue({
      id: "p_1",
      name: "Work",
      config: { agent_id: "ag_claude" },
    });
    const current = [catalogRow("h1", "claude-native", [{ id: "opus", displayName: "Opus" }])];
    let resolveFirstList!: (rows: unknown[]) => void;
    listCatalogsMock.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveFirstList = resolve;
        }),
    );
    listCatalogsMock.mockResolvedValue(current);
    syncCatalogsMock.mockResolvedValue(current);
    const { rerenderOpen } = renderDialog();
    await waitFor(() => expect(syncCatalogsMock).toHaveBeenCalledTimes(1));
    await act(async () => {});

    rerenderOpen(false);
    rerenderOpen(true);
    await waitFor(() => expect(listCatalogsMock).toHaveBeenCalledTimes(2));
    // The first opening's list answers last, with rows older than the sync's.
    await act(async () => {
      resolveFirstList([
        catalogRow("h1", "claude-native", [{ id: "opus[1m]", displayName: "Opus (1M context)" }]),
      ]);
    });

    await waitFor(() => expect(screen.getByTestId("project-settings-model")).toBeInTheDocument());
    fireEvent.click(screen.getByTestId("project-settings-model"));
    await waitFor(() =>
      expect(screen.getAllByRole("option").map((o) => o.textContent)).toEqual([
        "No default",
        "Opus",
      ]),
    );
  });

  it("drops a listed catalog the full sync no longer returns", async () => {
    availableAgentsMock.mockReturnValue({ data: [claudeAgent()] });
    getProjectMock.mockResolvedValue({
      id: "p_1",
      name: "Work",
      config: { agent_id: "ag_claude" },
    });
    const laptop = catalogRow("h1", "claude-native", [
      { id: "opus", displayName: "Opus" },
      { id: "sonnet", displayName: "Sonnet" },
    ]);
    // h2 was removed after the cached list was read; the sync omits it.
    listCatalogsMock.mockResolvedValue([
      laptop,
      catalogRow("h2", "claude-native", [{ id: "opus", displayName: "Opus" }]),
    ]);
    let resolveFullSync!: (rows: unknown[]) => void;
    syncCatalogsMock.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveFullSync = resolve;
        }),
    );
    renderDialog();
    await waitFor(() => expect(listCatalogsMock).toHaveBeenCalledTimes(1));
    // Let the list land first, so the stale h2 row is in state when the sync answers.
    await act(async () => {});
    await act(async () => {
      resolveFullSync([laptop]);
    });
    await waitFor(() => expect(screen.queryByText("Refreshing models…")).not.toBeInTheDocument());

    fireEvent.click(screen.getByTestId("project-settings-model"));
    await waitFor(() =>
      expect(screen.getAllByRole("option").map((o) => o.textContent)).toEqual([
        "No default",
        "Opus",
        "Sonnet",
      ]),
    );
  });

  it("keeps a per-host catalog synced while the open-time sync was running", async () => {
    hostsMock.mockReturnValue({
      data: [{ ...LAPTOP, configured_harnesses: { "claude-native": true } }],
    });
    availableAgentsMock.mockReturnValue({ data: [claudeAgent()] });
    getProjectMock.mockResolvedValue({ id: "p_1", name: "Work", config: {} });
    let resolveFullSync!: (rows: unknown[]) => void;
    syncCatalogsMock.mockImplementation((options?: { hostId?: string }) =>
      options?.hostId
        ? Promise.resolve([
            catalogRow("h1", "claude-native", [{ id: "opus", displayName: "Opus" }]),
          ])
        : new Promise((resolve) => {
            resolveFullSync = resolve;
          }),
    );
    renderDialog();
    await waitFor(() =>
      expect((screen.getByTestId("project-settings-save") as HTMLButtonElement).disabled).toBe(
        false,
      ),
    );
    fireEvent.click(screen.getByTestId("project-settings-add-host"));
    fireEvent.click(await screen.findByRole("option", { name: "Laptop" }));
    await pickOption("project-settings-host-agent-h1", "Claude Code");
    await pickOption("project-settings-host-model-h1", "Opus");

    // The full sync answers last and its snapshot lacks the pair.
    await act(async () => {
      resolveFullSync([]);
    });
    fireEvent.click(screen.getByTestId("project-settings-host-model-h1"));
    expect(await screen.findByRole("option", { name: "Opus" })).toBeInTheDocument();
  });

  it("hints Refreshing models… while the open-time sync is in flight", async () => {
    availableAgentsMock.mockReturnValue({ data: [claudeAgent()] });
    getProjectMock.mockResolvedValue({
      id: "p_1",
      name: "Work",
      config: { agent_id: "ag_claude" },
    });
    listCatalogsMock.mockResolvedValue([
      catalogRow("h1", "claude-native", [{ id: "opus", displayName: "Opus" }]),
    ]);
    let resolveSync!: (rows: unknown[]) => void;
    syncCatalogsMock.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveSync = resolve;
        }),
    );
    renderDialog();
    expect(await screen.findByText("Refreshing models…")).toBeInTheDocument();

    resolveSync([
      catalogRow("h1", "claude-native", [
        { id: "opus", displayName: "Opus" },
        { id: "sonnet", displayName: "Sonnet" },
      ]),
    ]);
    await waitFor(() => expect(screen.queryByText("Refreshing models…")).not.toBeInTheDocument());
    fireEvent.click(screen.getByTestId("project-settings-model"));
    await waitFor(() =>
      expect(screen.getAllByRole("option").map((o) => o.textContent)).toEqual([
        "No default",
        "Opus",
        "Sonnet",
      ]),
    );
  });

  it("shows both tabs and mounts the Code section without the feature flag", async () => {
    getProjectMock.mockResolvedValue({ id: "p_1", name: "Work", config: {} });
    renderDialog();
    await waitFor(() =>
      expect((screen.getByTestId("project-settings-save") as HTMLButtonElement).disabled).toBe(
        false,
      ),
    );

    expect(screen.getByRole("tab", { name: "Session defaults" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Code" })).toBeInTheDocument();
    fireEvent.mouseDown(screen.getByRole("tab", { name: "Code" }), { button: 0 });
    expect(await screen.findByTestId("project-code-section")).toHaveAttribute(
      "data-project-id",
      "p_1",
    );
    expect(screen.queryByTestId("project-settings-save")).not.toBeInTheDocument();
  });

  it("hints the code repository's default branch for the base branch", async () => {
    getCollaborationMock.mockResolvedValue({
      repositories: [
        {
          id: "r_1",
          project_id: "p_1",
          name: "web",
          role: "code",
          remote_url: "https://example.test/team/web.git",
          default_branch: "release",
          context_manifest_path: "",
          revision: 1,
          created_at: 1,
          updated_at: null,
        },
      ],
      bindings: [],
      problems: [],
    });
    getProjectMock.mockResolvedValue({ id: "p_1", name: "Work", config: {} });
    renderDialog();
    await waitFor(() =>
      expect((screen.getByTestId("project-settings-save") as HTMLButtonElement).disabled).toBe(
        false,
      ),
    );

    fireEvent.click(screen.getByTestId("project-settings-worktree"));
    expect(
      await screen.findByText(
        "Blank: the code repository's default branch (release), else the current branch.",
      ),
    ).toBeInTheDocument();
  });

  it("connects each tab to its labelled panel", async () => {
    getProjectMock.mockResolvedValue({ id: "p_1", name: "Work", config: {} });
    renderDialog();
    const tabs = screen.getAllByRole("tab");
    expect(tabs).toHaveLength(2);
    for (const tab of tabs) {
      const panel = document.getElementById(tab.getAttribute("aria-controls")!);
      expect(panel).toHaveAttribute("role", "tabpanel");
      expect(panel).toHaveAttribute("aria-labelledby", tab.id);
      expect(panel).toHaveAttribute("tabindex", "0");
    }
  });

  it("shows tab-specific actions and preserves the defaults draft", async () => {
    getProjectMock.mockResolvedValue({ id: "p_1", name: "Work", config: {} });
    const { rerenderOpen } = renderDialog();
    await waitFor(() =>
      expect((screen.getByTestId("project-settings-save") as HTMLButtonElement).disabled).toBe(
        false,
      ),
    );

    fireEvent.click(screen.getByTestId("project-settings-worktree"));
    fireEvent.mouseDown(screen.getByRole("tab", { name: "Code" }), { button: 0 });
    await waitFor(() => expect(screen.getByTestId("project-code-section")).toBeInTheDocument());
    expect(screen.queryByTestId("project-settings-save")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Done" })).toBeInTheDocument();
    fireEvent.mouseDown(screen.getByRole("tab", { name: "Session defaults" }), { button: 0 });
    expect(screen.getByTestId("project-settings-save")).toBeInTheDocument();
    expect(screen.getByTestId("project-settings-worktree")).toHaveAttribute(
      "data-state",
      "checked",
    );
    fireEvent.mouseDown(screen.getByRole("tab", { name: "Code" }), { button: 0 });
    rerenderOpen(false);
    rerenderOpen(true);
    await waitFor(() =>
      expect(screen.getByRole("tab", { name: "Session defaults" })).toHaveAttribute(
        "aria-selected",
        "true",
      ),
    );
    expect(screen.getByTestId("project-settings-save")).toBeInTheDocument();
  });

  it("keeps an edited default when a Code folder change refetches entries", async () => {
    getProjectMock.mockResolvedValue({ id: "p_1", name: "Work", config: {} });
    listEntriesMock.mockResolvedValue([entry("h1", "/old")]);
    const { client } = renderDialog();
    await waitFor(() =>
      expect((screen.getByTestId("project-settings-save") as HTMLButtonElement).disabled).toBe(
        false,
      ),
    );

    // An unsaved Session-defaults edit.
    fireEvent.click(screen.getByTestId("project-settings-worktree"));
    expect(screen.getByTestId("project-settings-worktree")).toHaveAttribute(
      "data-state",
      "checked",
    );

    // A folder change in the Code tab refetches the entries with new data.
    listEntriesMock.mockResolvedValue([entry("h1", "/new")]);
    await act(async () => {
      await client.invalidateQueries({ queryKey: ["project-entries", "p_1"] });
    });
    await waitFor(() => expect(listEntriesMock).toHaveBeenCalledTimes(2));

    // The refetch must not reset the edit from the stored config.
    expect(screen.getByTestId("project-settings-worktree")).toHaveAttribute(
      "data-state",
      "checked",
    );
  });

  // The per-host defaults editor: a list row per host with its directory and
  // a summary chip, the selected row's detail on the right, and the stored
  // `config.calling_defaults` round-tripped by Save.
  const codexSdkAgent = () =>
    pickerAgent({
      id: "ag_codex_sdk",
      name: "codex-sdk",
      display_name: "Codex SDK",
      harness: "codex",
    });

  it("renders the host list chip and round-trips the stored per-host set", async () => {
    hostsMock.mockReturnValue({ data: [LAPTOP, DESKTOP] });
    availableAgentsMock.mockReturnValue({ data: [claudeAgent(), codexSdkAgent()] });
    listEntriesMock.mockResolvedValue([entry("h1", "/repo/one")]);
    getProjectMock.mockResolvedValue({
      id: "p_1",
      name: "Work",
      config: {
        calling_defaults: {
          h1: {
            agent_id: "ag_codex_sdk",
            harnesses: { codex: { model: "gpt-6-sol", effort: "high" } },
          },
        },
      },
    });
    const catalogRows = [
      catalogRow("h1", "codex", [{ id: "gpt-6-sol", displayName: "GPT-6-Sol" }]),
    ];
    listCatalogsMock.mockResolvedValue(catalogRows);
    // The dialog's open-time full sync replaces the listed rows, so it must
    // return them too.
    syncCatalogsMock.mockResolvedValue(catalogRows);
    renderDialog();

    await waitFor(() =>
      expect(screen.getByTestId("project-settings-host-summary-h1")).toHaveTextContent(
        "Codex SDK · GPT-6-Sol · High",
      ),
    );
    // The first row is selected, so its detail shows the set.
    expect(screen.getByTestId("project-settings-host-model-h1")).toHaveTextContent("GPT-6-Sol");
    expect(screen.getByTestId("project-settings-host-effort-h1")).toHaveTextContent("High");

    fireEvent.click(screen.getByTestId("project-settings-save"));
    await waitFor(() =>
      expect(updateMock).toHaveBeenCalledWith("p_1", {
        calling_defaults: {
          h1: {
            agent_id: "ag_codex_sdk",
            harnesses: { codex: { model: "gpt-6-sol", effort: "high" } },
          },
        },
      }),
    );
  });

  it("adds a host and edits its agent / model / effort (one sync per pair)", async () => {
    hostsMock.mockReturnValue({
      data: [{ ...LAPTOP, configured_harnesses: { "claude-native": true, "codex-native": false } }],
    });
    availableAgentsMock.mockReturnValue({
      data: [
        claudeAgent(),
        pickerAgent({
          id: "ag_codex",
          name: "codex-native-ui",
          display_name: "Codex",
          harness: "codex-native",
        }),
      ],
    });
    getProjectMock.mockResolvedValue({ id: "p_1", name: "Work", config: {} });
    // The open-time full sync (no filter) returns nothing; only the per-pair
    // sync below fills (h1, claude-native).
    syncCatalogsMock.mockImplementation(async (options?: { hostId?: string }) =>
      options?.hostId
        ? [catalogRow("h1", "claude-native", [{ id: "opus", displayName: "Opus" }])]
        : [],
    );
    renderDialog();
    await waitFor(() =>
      expect((screen.getByTestId("project-settings-save") as HTMLButtonElement).disabled).toBe(
        false,
      ),
    );

    fireEvent.click(screen.getByTestId("project-settings-add-host"));
    fireEvent.click(await screen.findByRole("option", { name: "Laptop" }));

    // Only agents usable on this host are offered: Claude Code is ready, the
    // host-reported-unready Codex is not.
    fireEvent.click(screen.getByTestId("project-settings-host-agent-h1"));
    expect(await screen.findByRole("option", { name: "Claude Code" })).toBeInTheDocument();
    expect(screen.queryByRole("option", { name: "Codex" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("option", { name: "Claude Code" }));

    // Opening the model dropdown syncs the missing (h1, claude-native) pair
    // once; the now-cached pair is not re-synced by the sibling dropdown.
    // The open-time full sync also calls the mock, so count the filtered
    // (per-pair) calls specifically.
    const pairSyncs = () =>
      syncCatalogsMock.mock.calls.filter(([options]) => options !== undefined);
    await pickOption("project-settings-host-model-h1", "Opus");
    expect(pairSyncs()).toHaveLength(1);
    expect(syncCatalogsMock).toHaveBeenCalledWith({ hostId: "h1", harness: "claude-native" });
    fireEvent.click(screen.getByTestId("project-settings-host-effort-h1"));
    fireEvent.click(await screen.findByRole("option", { name: "High" }));
    expect(pairSyncs()).toHaveLength(1);

    fireEvent.click(screen.getByTestId("project-settings-save"));
    await waitFor(() =>
      expect(updateMock).toHaveBeenCalledWith("p_1", {
        calling_defaults: {
          h1: {
            agent_id: "ag_claude",
            harnesses: { "claude-native": { model: "opus", effort: "high" } },
          },
        },
      }),
    );
  });

  it("clears model and effort with Default without storing a clear word", async () => {
    availableAgentsMock.mockReturnValue({ data: [codexSdkAgent()] });
    listEntriesMock.mockResolvedValue([entry("h1", "/repo")]);
    getProjectMock.mockResolvedValue({
      id: "p_1",
      name: "Work",
      config: {
        calling_defaults: {
          h1: {
            agent_id: "ag_codex_sdk",
            harnesses: { codex: { model: "gpt-6-sol", effort: "high" } },
          },
        },
      },
    });
    const catalogRows = [
      catalogRow("h1", "codex", [{ id: "gpt-6-sol", displayName: "GPT-6-Sol" }]),
    ];
    listCatalogsMock.mockResolvedValue(catalogRows);
    // The dialog's open-time full sync replaces the listed rows, so it must
    // return them too.
    syncCatalogsMock.mockResolvedValue(catalogRows);
    renderDialog();
    await waitFor(() =>
      expect(screen.getByTestId("project-settings-host-summary-h1")).toHaveTextContent(
        "Codex SDK · GPT-6-Sol · High",
      ),
    );

    await pickOption("project-settings-host-model-h1", "Default");
    await pickOption("project-settings-host-effort-h1", "Default");
    fireEvent.click(screen.getByTestId("project-settings-save"));
    await waitFor(() =>
      expect(updateMock).toHaveBeenCalledWith("p_1", {
        calling_defaults: { h1: { agent_id: "ag_codex_sdk" } },
      }),
    );
  });

  // Inside the dialog's <form>, Radix mirrors each Select into a hidden native
  // <select>; a value it has no option for yet must not come back as a pick.
  it("keeps a host's stored agent and model when its row is selected", async () => {
    hostsMock.mockReturnValue({
      data: [{ ...LAPTOP, configured_harnesses: { "claude-native": false } }, DESKTOP],
    });
    availableAgentsMock.mockReturnValue({ data: [claudeAgent(), codexSdkAgent()] });
    listEntriesMock.mockResolvedValue([entry("h1", "/repo/one"), entry("h2", "/repo/two")]);
    const callingDefaults = {
      h1: {
        agent_id: "ag_codex_sdk",
        harnesses: { codex: { model: "gpt-6-sol", effort: "high" } },
      },
      h2: {
        agent_id: "ag_claude",
        harnesses: { "claude-native": { model: "opus", effort: "xhigh" } },
      },
    };
    getProjectMock.mockResolvedValue({
      id: "p_1",
      name: "Work",
      config: { calling_defaults: callingDefaults },
    });
    const catalogRows = [
      catalogRow("h1", "codex", [{ id: "gpt-6-sol", displayName: "GPT-6-Sol" }]),
      catalogRow("h2", "claude-native", [{ id: "opus", displayName: "Opus" }]),
    ];
    listCatalogsMock.mockResolvedValue(catalogRows);
    syncCatalogsMock.mockResolvedValue(catalogRows);
    renderDialog();
    await waitFor(() =>
      expect(screen.getByTestId("project-settings-host-summary-h2")).toHaveTextContent(
        "Claude Code · Opus · xHigh",
      ),
    );

    fireEvent.click(screen.getByTestId("project-settings-entry-h2"));
    await screen.findByTestId("project-settings-host-detail-h2");

    expect(screen.getByTestId("project-settings-host-summary-h2")).toHaveTextContent(
      "Claude Code · Opus · xHigh",
    );
    expect(screen.getByTestId("project-settings-host-agent-h2")).toHaveTextContent("Claude Code");
    expect(screen.getByTestId("project-settings-host-model-h2")).toHaveTextContent("Opus");
    fireEvent.click(screen.getByTestId("project-settings-save"));
    await waitFor(() =>
      expect(updateMock).toHaveBeenCalledWith("p_1", { calling_defaults: callingDefaults }),
    );
  });

  it("shows the new harness's stored model when a host row's agent changes", async () => {
    availableAgentsMock.mockReturnValue({ data: [claudeAgent(), codexSdkAgent()] });
    listEntriesMock.mockResolvedValue([entry("h1", "/repo/one")]);
    getProjectMock.mockResolvedValue({
      id: "p_1",
      name: "Work",
      config: {
        calling_defaults: {
          h1: {
            agent_id: "ag_codex_sdk",
            harnesses: {
              codex: { model: "gpt-6-sol", effort: "high" },
              "claude-native": { model: "opus" },
            },
          },
        },
      },
    });
    const catalogRows = [
      catalogRow("h1", "codex", [{ id: "gpt-6-sol", displayName: "GPT-6-Sol" }]),
      catalogRow("h1", "claude-native", [{ id: "opus", displayName: "Opus" }]),
    ];
    listCatalogsMock.mockResolvedValue(catalogRows);
    syncCatalogsMock.mockResolvedValue(catalogRows);
    renderDialog();
    await waitFor(() =>
      expect(screen.getByTestId("project-settings-host-model-h1")).toHaveTextContent("GPT-6-Sol"),
    );

    await pickOption("project-settings-host-agent-h1", "Claude Code");

    expect(screen.getByTestId("project-settings-host-model-h1")).toHaveTextContent("Opus");
    fireEvent.click(screen.getByTestId("project-settings-save"));
    await waitFor(() =>
      expect(updateMock).toHaveBeenCalledWith("p_1", {
        calling_defaults: {
          h1: {
            agent_id: "ag_claude",
            harnesses: {
              codex: { model: "gpt-6-sol", effort: "high" },
              "claude-native": { model: "opus" },
            },
          },
        },
      }),
    );
  });

  it("removes a host row's calling_defaults without touching its project folder", async () => {
    hostsMock.mockReturnValue({ data: [LAPTOP, DESKTOP] });
    availableAgentsMock.mockReturnValue({ data: [claudeAgent()] });
    listEntriesMock.mockResolvedValue([entry("h1", "/repo/one"), entry("h2", "/repo/two")]);
    getProjectMock.mockResolvedValue({
      id: "p_1",
      name: "Work",
      config: {
        calling_defaults: {
          h1: { agent_id: "ag_claude" },
          h2: { agent_id: "ag_claude", harnesses: { "claude-native": { model: "opus" } } },
        },
      },
    });
    renderDialog();
    await waitFor(() =>
      expect(screen.getByTestId("project-settings-entry-h2")).toBeInTheDocument(),
    );

    fireEvent.click(screen.getByTestId("project-settings-entry-remove-h2"));
    fireEvent.click(screen.getByTestId("project-settings-save"));

    await waitFor(() =>
      expect(updateMock).toHaveBeenCalledWith("p_1", {
        calling_defaults: { h1: { agent_id: "ag_claude" } },
      }),
    );
    // The folder is the Code tab's to remove; this form only drops the set.
    expect(deleteEntryMock).not.toHaveBeenCalled();
    expect(putEntryMock).not.toHaveBeenCalled();
  });

  it("hides model and effort for a joint agent and says Set by members", async () => {
    availableAgentsMock.mockReturnValue({
      data: [
        pickerAgent({
          id: "ca_joint",
          name: "my-joint",
          display_name: "My Joint",
          harness: "codex",
        }),
      ],
    });
    listEntriesMock.mockResolvedValue([entry("h1", "/repo")]);
    getProjectMock.mockResolvedValue({
      id: "p_1",
      name: "Work",
      config: { calling_defaults: { h1: { agent_id: "ca_joint" } } },
    });
    renderDialog();

    await waitFor(() =>
      expect(screen.getByTestId("project-settings-host-members-h1")).toHaveTextContent(
        "Set by members",
      ),
    );
    expect(screen.getByTestId("project-settings-host-summary-h1")).toHaveTextContent(
      "My Joint · Set by members",
    );
    expect(screen.queryByTestId("project-settings-host-model-h1")).not.toBeInTheDocument();
    expect(screen.queryByTestId("project-settings-host-effort-h1")).not.toBeInTheDocument();

    fireEvent.click(screen.getByTestId("project-settings-save"));
    await waitFor(() =>
      expect(updateMock).toHaveBeenCalledWith("p_1", {
        calling_defaults: { h1: { agent_id: "ca_joint" } },
      }),
    );
  });

  it("moves the legacy agent / model into the All hosts row and round-trips them", async () => {
    availableAgentsMock.mockReturnValue({ data: [claudeAgent()] });
    const catalogRows = [catalogRow("h1", "claude-native", [{ id: "opus", displayName: "Opus" }])];
    listCatalogsMock.mockResolvedValue(catalogRows);
    syncCatalogsMock.mockResolvedValue(catalogRows);
    listEntriesMock.mockResolvedValue([entry("h1", "/repo")]);
    getProjectMock.mockResolvedValue({
      id: "p_1",
      name: "Work",
      config: { agent_id: "ag_claude", model: "opus" },
    });
    renderDialog();
    await waitFor(() =>
      expect(screen.getByTestId("project-settings-entry-h1")).toBeInTheDocument(),
    );
    // The host detail is selected, not the legacy fallback.
    expect(screen.queryByTestId("project-settings-agent")).not.toBeInTheDocument();

    fireEvent.click(screen.getByTestId("project-settings-all-hosts"));
    expect(screen.getByTestId("project-settings-agent")).toBeInTheDocument();
    expect(screen.getByTestId("project-settings-model")).toBeInTheDocument();

    fireEvent.click(screen.getByTestId("project-settings-save"));
    await waitFor(() =>
      expect(updateMock).toHaveBeenCalledWith("p_1", { agent_id: "ag_claude", model: "opus" }),
    );
  });

  it("truncates a 200-character directory with the full path in the title", async () => {
    const longPath = `/${"a".repeat(199)}`;
    listEntriesMock.mockResolvedValue([entry("h1", longPath)]);
    getProjectMock.mockResolvedValue({ id: "p_1", name: "Work", config: {} });
    renderDialog();

    const pathEl = await screen.findByTestId("project-settings-host-path-h1");
    expect(pathEl).toHaveTextContent(longPath);
    expect(pathEl).toHaveAttribute("title", longPath);
    expect(pathEl.className).toContain("truncate");
  });

  it("opens one host detail at a time in the accordion below md", async () => {
    const restoreViewport = useMobileViewport();
    try {
      hostsMock.mockReturnValue({ data: [LAPTOP, DESKTOP] });
      listEntriesMock.mockResolvedValue([entry("h1", "/one"), entry("h2", "/two")]);
      getProjectMock.mockResolvedValue({ id: "p_1", name: "Work", config: {} });
      renderDialog();

      // The first row starts expanded; the second stays collapsed.
      expect(await screen.findByTestId("project-settings-host-detail-h1")).toBeInTheDocument();
      expect(screen.queryByTestId("project-settings-host-detail-h2")).not.toBeInTheDocument();

      fireEvent.click(screen.getByTestId("project-settings-entry-h2"));
      expect(screen.getByTestId("project-settings-host-detail-h2")).toBeInTheDocument();
      expect(screen.queryByTestId("project-settings-host-detail-h1")).not.toBeInTheDocument();

      // Tapping the open row collapses it.
      fireEvent.click(screen.getByTestId("project-settings-entry-h2"));
      expect(screen.queryByTestId("project-settings-host-detail-h2")).not.toBeInTheDocument();
    } finally {
      restoreViewport();
    }
  });

  it("adds and deletes an other-harness model row", async () => {
    availableAgentsMock.mockReturnValue({ data: [claudeAgent()] });
    listEntriesMock.mockResolvedValue([entry("h1", "/repo")]);
    getProjectMock.mockResolvedValue({
      id: "p_1",
      name: "Work",
      config: {
        calling_defaults: {
          h1: { agent_id: "ag_claude", harnesses: { "claude-native": { model: "opus" } } },
        },
      },
    });
    syncCatalogsMock.mockResolvedValue([
      catalogRow("h1", "codex", [{ id: "gpt-6-sol", displayName: "GPT-6-Sol" }]),
    ]);
    renderDialog();
    await waitFor(() =>
      expect(screen.getByTestId("project-settings-host-detail-h1")).toBeInTheDocument(),
    );

    fireEvent.click(screen.getByTestId("project-settings-host-other-toggle-h1"));
    fireEvent.click(screen.getByTestId("project-settings-host-other-add-h1"));
    fireEvent.click(await screen.findByRole("option", { name: "Codex SDK" }));
    await pickOption("project-settings-host-other-model-h1-codex", "GPT-6-Sol");
    expect(screen.getByTestId("project-settings-host-other-h1-codex")).toBeInTheDocument();

    // Deleting the row omits its harness entry from the next save.
    fireEvent.click(screen.getByTestId("project-settings-host-other-remove-h1-codex"));
    expect(screen.queryByTestId("project-settings-host-other-h1-codex")).not.toBeInTheDocument();

    fireEvent.click(screen.getByTestId("project-settings-save"));
    await waitFor(() =>
      expect(updateMock).toHaveBeenCalledWith("p_1", {
        calling_defaults: {
          h1: { agent_id: "ag_claude", harnesses: { "claude-native": { model: "opus" } } },
        },
      }),
    );
  });
});
