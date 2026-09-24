import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { ProjectSettingsDialog } from "./ProjectSettingsDialog";
import {
  createProject,
  deleteProjectEntry,
  getProject,
  getProjectCollaboration,
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
  listProjectEntries: vi.fn(),
  putProjectEntry: vi.fn(),
  setProjectCollaborationEnabled: vi.fn(),
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
} = vi.hoisted(() => ({
  hostsMock: vi.fn(),
  availableAgentsMock: vi.fn(),
  hostModelOptionsMock: vi.fn(),
  serverInfoMock: vi.fn(),
  workspacePickerPropsMock: vi.fn(),
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
// The filesystem browser owns its own data-fetching; stub it to a marker plus
// a button that reports a navigated path, so we can drive the disclosure and
// the live workspace update without the host-filesystem plumbing.
vi.mock("./WorkspacePicker", () => ({
  isNavigablePath: (p: string) => p.startsWith("/"),
  HostWorkspacePicker: (props: { onNavigate: (p: string) => void }) => {
    workspacePickerPropsMock(props);
    return (
      <div data-testid="mock-workspace-picker">
        <button type="button" onClick={() => props.onNavigate("/picked/dir")}>
          pick dir
        </button>
      </div>
    );
  },
}));

const getProjectMock = vi.mocked(getProject);
const getCollaborationMock = vi.mocked(getProjectCollaboration);
const updateMock = vi.mocked(updateProjectConfig);
const createMock = vi.mocked(createProject);
const listEntriesMock = vi.mocked(listProjectEntries);
const putEntryMock = vi.mocked(putProjectEntry);
const deleteEntryMock = vi.mocked(deleteProjectEntry);

function entry(hostId: string, workspace: string) {
  return { host_id: hostId, workspace, updated_at: null };
}

function renderDialog(projectId: string | null = "p_1", onOpenChangeSpy?: (open: boolean) => void) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const onOpenChange = onOpenChangeSpy ?? vi.fn();
  const view = (open: boolean) => (
    <QueryClientProvider client={client}>
      <TooltipProvider>
        <ProjectSettingsDialog
          open={open}
          onOpenChange={onOpenChange}
          projectId={projectId}
          projectName="Work"
        />
      </TooltipProvider>
    </QueryClientProvider>
  );
  const result = render(view(true));
  return { ...result, onOpenChange, rerenderOpen: (open: boolean) => result.rerender(view(open)) };
}

beforeEach(() => {
  getProjectMock.mockReset();
  getCollaborationMock.mockReset();
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
  hostsMock.mockReturnValue({ data: [LAPTOP] });
  availableAgentsMock.mockReturnValue({ data: [pickerAgent()] });
  hostModelOptionsMock.mockReturnValue({ data: [] });
  serverInfoMock.mockReturnValue({
    managed_sandboxes_enabled: false,
    sandbox_provider: null,
    features: {},
  });
  listEntriesMock.mockResolvedValue([]);
  putEntryMock.mockImplementation(async (_id, hostId, workspace) => entry(hostId, workspace));
  deleteEntryMock.mockResolvedValue(undefined);
  updateMock.mockResolvedValue({ id: "p_1", name: "Work", config: {} });
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
    // The config's workspace seeds as a row on the default host; Save then
    // turns it into a real entry and mirrors it back into the config.
    expect(screen.getByTestId("project-settings-entry-h1")).toHaveTextContent("/repo");
    fireEvent.click(screen.getByTestId("project-settings-save"));
    await waitFor(() =>
      expect(updateMock).toHaveBeenCalledWith("p_1", {
        host_id: "h1",
        workspace: "/repo",
        agent_id: "ag_1",
      }),
    );
    expect(putEntryMock).toHaveBeenCalledWith("p_1", "h1", "/repo");
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

  it("shows the config workspace as a row when the project has no entry yet", async () => {
    getProjectMock.mockResolvedValue({
      id: "p_1",
      name: "Work",
      config: { host_id: "h1", workspace: "/legacy/repo" },
    });
    renderDialog();

    await waitFor(() =>
      expect(screen.getByTestId("project-settings-entry-h1")).toHaveTextContent("/legacy/repo"),
    );
    // Save promotes the fallback row into a real entry.
    fireEvent.click(screen.getByTestId("project-settings-save"));
    await waitFor(() => expect(putEntryMock).toHaveBeenCalledWith("p_1", "h1", "/legacy/repo"));
    expect(updateMock).toHaveBeenCalledWith("p_1", { host_id: "h1", workspace: "/legacy/repo" });
  });

  it("saves changed rows, deletes removed rows, then mirrors the default host's row", async () => {
    hostsMock.mockReturnValue({ data: [LAPTOP, DESKTOP, SERVER] });
    listEntriesMock.mockResolvedValue([entry("h1", "/old"), entry("h2", "/stale")]);
    getProjectMock.mockResolvedValue({ id: "p_1", name: "Work", config: { host_id: "h1" } });
    renderDialog();
    await waitFor(() =>
      expect(screen.getByTestId("project-settings-entry-h1")).toHaveTextContent("/old"),
    );

    // Change h1's path through the browser, remove h2, add the offline server
    // with a typed path.
    fireEvent.click(screen.getByTestId("project-settings-entry-browse-h1"));
    fireEvent.click(screen.getByText("pick dir"));
    fireEvent.click(screen.getByTestId("project-settings-entry-remove-h2"));
    fireEvent.click(screen.getByTestId("project-settings-add-host"));
    fireEvent.click(screen.getByRole("option", { name: "Build server" }));
    fireEvent.change(screen.getByTestId("project-settings-entry-path-h3"), {
      target: { value: "/srv/repo" },
    });

    fireEvent.click(screen.getByTestId("project-settings-save"));

    await waitFor(() => expect(updateMock).toHaveBeenCalled());
    expect(putEntryMock.mock.calls).toEqual([
      ["p_1", "h1", "/picked/dir"],
      ["p_1", "h3", "/srv/repo"],
    ]);
    expect(deleteEntryMock).toHaveBeenCalledWith("p_1", "h2");
    expect(updateMock).toHaveBeenCalledWith("p_1", { host_id: "h1", workspace: "/picked/dir" });
    // PUTs, then the DELETE, then the config PATCH — sequentially.
    const order = (mock: { mock: { invocationCallOrder: number[] } }) =>
      mock.mock.invocationCallOrder[0];
    expect(order(putEntryMock)).toBeLessThan(order(deleteEntryMock));
    expect(order(deleteEntryMock)).toBeLessThan(order(updateMock));
  });

  it("stops at the first row error and writes no config", async () => {
    hostsMock.mockReturnValue({ data: [LAPTOP, DESKTOP] });
    listEntriesMock.mockResolvedValue([entry("h1", "/old"), entry("h2", "/stale")]);
    getProjectMock.mockResolvedValue({ id: "p_1", name: "Work", config: { host_id: "h1" } });
    const onOpenChange = vi.fn();
    // The first PUT fails (offline host) — the rest of the sequence must stop.
    putEntryMock.mockRejectedValue(new Error("host is offline"));
    renderDialog(undefined, onOpenChange);
    await waitFor(() =>
      expect(screen.getByTestId("project-settings-entry-h1")).toHaveTextContent("/old"),
    );

    fireEvent.click(screen.getByTestId("project-settings-entry-browse-h1"));
    fireEvent.click(screen.getByText("pick dir"));
    fireEvent.click(screen.getByTestId("project-settings-entry-remove-h2"));
    fireEvent.click(screen.getByTestId("project-settings-save"));

    await waitFor(() =>
      expect(screen.getByTestId("project-settings-entry-error-h1")).toHaveTextContent(
        "host is offline",
      ),
    );
    expect(deleteEntryMock).not.toHaveBeenCalled();
    expect(updateMock).not.toHaveBeenCalled();
    expect(onOpenChange).not.toHaveBeenCalledWith(false);
  });

  it("reports a failed removed-row DELETE after the row is gone", async () => {
    hostsMock.mockReturnValue({ data: [LAPTOP, DESKTOP] });
    listEntriesMock.mockResolvedValue([entry("h1", "/old"), entry("h2", "/stale")]);
    getProjectMock.mockResolvedValue({ id: "p_1", name: "Work", config: { host_id: "h1" } });
    deleteEntryMock.mockRejectedValue(new Error("Entry not found"));
    renderDialog();
    await waitFor(() =>
      expect(screen.getByTestId("project-settings-entry-h2")).toBeInTheDocument(),
    );

    fireEvent.click(screen.getByTestId("project-settings-entry-remove-h2"));
    fireEvent.click(screen.getByTestId("project-settings-save"));

    await waitFor(() =>
      expect(screen.getByTestId("project-settings-entries-error")).toHaveTextContent(
        "Entry not found",
      ),
    );
    expect(updateMock).not.toHaveBeenCalled();
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

  it("opens a row's directory browser, updates its path, then closes on outside click", async () => {
    listEntriesMock.mockResolvedValue([entry("h1", "/repo")]);
    getProjectMock.mockResolvedValue({ id: "p_1", name: "Work", config: { host_id: "h1" } });
    renderDialog();
    await waitFor(() =>
      expect(screen.getByTestId("project-settings-entry-browse-h1")).toHaveTextContent("/repo"),
    );

    // Expand → the browser mounts against the row's host; navigating updates
    // the trigger label live.
    fireEvent.click(screen.getByTestId("project-settings-entry-browse-h1"));
    expect(screen.getByTestId("mock-workspace-picker")).toBeInTheDocument();
    expect(workspacePickerPropsMock).toHaveBeenLastCalledWith(
      expect.objectContaining({ hostId: "h1", initialPath: "/repo" }),
    );
    fireEvent.click(screen.getByText("pick dir"));
    expect(screen.getByTestId("project-settings-entry-browse-h1")).toHaveTextContent(
      "/picked/dir",
    );

    // The click-away backdrop closes the browser, keeping the picked path.
    fireEvent.click(screen.getByRole("button", { name: /close directory browser/i }));
    expect(screen.queryByTestId("mock-workspace-picker")).not.toBeInTheDocument();

    fireEvent.click(screen.getByTestId("project-settings-save"));
    await waitFor(() => expect(putEntryMock).toHaveBeenCalledWith("p_1", "h1", "/picked/dir"));
    expect(updateMock).toHaveBeenCalledWith("p_1", { host_id: "h1", workspace: "/picked/dir" });
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
    // "Custom agents" submenu where composed agents are listed.
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

  it("hides the collaboration section when the project_assignments feature is off", async () => {
    getProjectMock.mockResolvedValue({ id: "p_1", name: "Work", config: {} });
    renderDialog();
    await waitFor(() =>
      expect((screen.getByTestId("project-settings-save") as HTMLButtonElement).disabled).toBe(
        false,
      ),
    );

    expect(screen.queryByTestId("project-collaboration-section")).not.toBeInTheDocument();
    expect(screen.queryByTestId("project-collaboration-enabled")).not.toBeInTheDocument();
    expect(getCollaborationMock).not.toHaveBeenCalled();
    expect(screen.queryByRole("tablist")).not.toBeInTheDocument();
    expect(document.getElementById("project-settings-defaults-form")).not.toHaveAttribute("role");
  });

  it("connects each tab to its labelled panel when collaboration is enabled", async () => {
    serverInfoMock.mockReturnValue({
      managed_sandboxes_enabled: false,
      sandbox_provider: null,
      features: { project_assignments: true },
    });
    getProjectMock.mockResolvedValue({ id: "p_1", name: "Work", config: {} });
    getCollaborationMock.mockResolvedValue({
      enabled: false,
      revision: 1,
      repositories: [],
      bindings: [],
      problems: [],
    });
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
    serverInfoMock.mockReturnValue({
      managed_sandboxes_enabled: false,
      sandbox_provider: null,
      features: { project_assignments: true },
    });
    getProjectMock.mockResolvedValue({ id: "p_1", name: "Work", config: {} });
    getCollaborationMock.mockResolvedValue({
      enabled: false,
      revision: 1,
      repositories: [],
      bindings: [],
      problems: [],
    });
    const { rerenderOpen } = renderDialog();
    await waitFor(() =>
      expect((screen.getByTestId("project-settings-save") as HTMLButtonElement).disabled).toBe(
        false,
      ),
    );

    fireEvent.click(screen.getByTestId("project-settings-worktree"));
    fireEvent.mouseDown(screen.getByRole("tab", { name: "Collaboration" }), { button: 0 });
    await waitFor(() =>
      expect(screen.getByTestId("project-collaboration-enabled")).toBeInTheDocument(),
    );
    expect(screen.queryByTestId("project-settings-save")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Done" })).toBeInTheDocument();
    fireEvent.mouseDown(screen.getByRole("tab", { name: "Session defaults" }), { button: 0 });
    expect(screen.getByTestId("project-settings-save")).toBeInTheDocument();
    expect(screen.getByTestId("project-settings-worktree")).toHaveAttribute(
      "data-state",
      "checked",
    );
    fireEvent.mouseDown(screen.getByRole("tab", { name: "Collaboration" }), { button: 0 });
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

  it("preserves the repository draft when switching away from Collaboration", async () => {
    serverInfoMock.mockReturnValue({
      managed_sandboxes_enabled: false,
      sandbox_provider: null,
      features: { project_assignments: true },
    });
    getProjectMock.mockResolvedValue({ id: "p_1", name: "Work", config: {} });
    getCollaborationMock.mockResolvedValue({
      enabled: false,
      revision: 1,
      repositories: [],
      bindings: [],
      problems: [],
    });
    renderDialog();
    fireEvent.mouseDown(screen.getByRole("tab", { name: "Collaboration" }), { button: 0 });
    await waitFor(() =>
      expect(screen.getByTestId("project-collaboration-repo-open")).toBeInTheDocument(),
    );
    fireEvent.click(screen.getByTestId("project-collaboration-repo-open"));
    fireEvent.change(screen.getByTestId("project-collaboration-repo-url"), {
      target: { value: "https://example.com/draft.git" },
    });
    fireEvent.mouseDown(screen.getByRole("tab", { name: "Session defaults" }), { button: 0 });
    fireEvent.mouseDown(screen.getByRole("tab", { name: "Collaboration" }), { button: 0 });
    expect(screen.getByTestId("project-collaboration-repo-url")).toHaveValue(
      "https://example.com/draft.git",
    );
  });
});
