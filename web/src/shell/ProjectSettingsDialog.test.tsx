import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
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
import { ApiError } from "@/lib/sessionsApi";

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
  return {
    ...result,
    client,
    onOpenChange,
    rerenderOpen: (open: boolean) => result.rerender(view(open)),
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
    deleteEntryMock.mockRejectedValue(new ApiError("host is unreachable", 502, null));
    renderDialog();
    await waitFor(() =>
      expect(screen.getByTestId("project-settings-entry-h2")).toBeInTheDocument(),
    );

    fireEvent.click(screen.getByTestId("project-settings-entry-remove-h2"));
    fireEvent.click(screen.getByTestId("project-settings-save"));

    await waitFor(() =>
      expect(screen.getByTestId("project-settings-entries-error")).toHaveTextContent(
        "host is unreachable",
      ),
    );
    expect(updateMock).not.toHaveBeenCalled();
  });

  it("resumes a partial save without repeating a DELETE that already landed", async () => {
    hostsMock.mockReturnValue({ data: [LAPTOP, DESKTOP] });
    listEntriesMock.mockResolvedValue([entry("h1", "/old"), entry("h2", "/stale")]);
    getProjectMock.mockResolvedValue({ id: "p_1", name: "Work", config: { host_id: "h1" } });
    // The DELETE lands; the config PATCH fails. A retry must not repeat the
    // DELETE (which would now 404) and must still send the PATCH.
    updateMock.mockRejectedValueOnce(new Error("config unavailable"));
    renderDialog();
    await waitFor(() =>
      expect(screen.getByTestId("project-settings-entry-h2")).toBeInTheDocument(),
    );

    fireEvent.click(screen.getByTestId("project-settings-entry-remove-h2"));
    fireEvent.click(screen.getByTestId("project-settings-save"));

    await waitFor(() => expect(updateMock).toHaveBeenCalledTimes(1));
    expect(deleteEntryMock).toHaveBeenCalledTimes(1);
    expect(deleteEntryMock).toHaveBeenCalledWith("p_1", "h2");

    fireEvent.click(screen.getByTestId("project-settings-save"));

    await waitFor(() => expect(updateMock).toHaveBeenCalledTimes(2));
    // The successful DELETE is now part of the saved baseline, so the retry
    // sends only what is still pending.
    expect(deleteEntryMock).toHaveBeenCalledTimes(1);
  });

  it("treats a 404 on an entry DELETE as already done", async () => {
    hostsMock.mockReturnValue({ data: [LAPTOP, DESKTOP] });
    listEntriesMock.mockResolvedValue([entry("h1", "/old"), entry("h2", "/stale")]);
    getProjectMock.mockResolvedValue({ id: "p_1", name: "Work", config: { host_id: "h1" } });
    const { onOpenChange } = renderDialog();
    await waitFor(() =>
      expect(screen.getByTestId("project-settings-entry-h2")).toBeInTheDocument(),
    );
    deleteEntryMock.mockRejectedValue(new ApiError("Entry not found", 404, "NOT_FOUND"));

    fireEvent.click(screen.getByTestId("project-settings-entry-remove-h2"));
    fireEvent.click(screen.getByTestId("project-settings-save"));

    // The row is already gone → the save continues to the config PATCH.
    await waitFor(() => expect(updateMock).toHaveBeenCalled());
    expect(screen.queryByTestId("project-settings-entries-error")).not.toBeInTheDocument();
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });

  it("keeps the dialog open with a row warning when the post-bind command fails", async () => {
    listEntriesMock.mockResolvedValue([entry("h1", "/old")]);
    getProjectMock.mockResolvedValue({ id: "p_1", name: "Work", config: { host_id: "h1" } });
    const onOpenChange = vi.fn();
    putEntryMock.mockResolvedValue({
      ...entry("h1", "/new"),
      post_bind: { status: "failed", exit_code: 3, output: "boom", error: "command exited 3" },
    });
    renderDialog("p_1", onOpenChange);
    await waitFor(() =>
      expect(screen.getByTestId("project-settings-entry-h1")).toHaveTextContent("/old"),
    );

    fireEvent.click(screen.getByTestId("project-settings-entry-browse-h1"));
    fireEvent.click(screen.getByText("pick dir"));
    fireEvent.click(screen.getByTestId("project-settings-save"));

    await waitFor(() =>
      expect(screen.getByTestId("project-settings-entry-post-bind-h1")).toHaveTextContent(
        /Directory saved; post-bind command failed/,
      ),
    );
    const warning = screen.getByTestId("project-settings-entry-post-bind-h1");
    expect(warning).toHaveTextContent("exit code 3");
    expect(warning).toHaveTextContent("boom");
    expect(onOpenChange).not.toHaveBeenCalledWith(false);
    // The row and the config mirror still saved; only the hook failed.
    expect(updateMock).toHaveBeenCalledWith("p_1", { host_id: "h1", workspace: "/picked/dir" });
  });

  it("closes the dialog when the post-bind command succeeds", async () => {
    listEntriesMock.mockResolvedValue([entry("h1", "/old")]);
    getProjectMock.mockResolvedValue({ id: "p_1", name: "Work", config: { host_id: "h1" } });
    const onOpenChange = vi.fn();
    putEntryMock.mockResolvedValue({
      ...entry("h1", "/new"),
      post_bind: { status: "ok", exit_code: 0, output: "skip: no .collab-root", error: null },
    });
    renderDialog("p_1", onOpenChange);
    await waitFor(() =>
      expect(screen.getByTestId("project-settings-entry-h1")).toHaveTextContent("/old"),
    );

    fireEvent.click(screen.getByTestId("project-settings-entry-browse-h1"));
    fireEvent.click(screen.getByText("pick dir"));
    fireEvent.click(screen.getByTestId("project-settings-save"));

    await waitFor(() => expect(onOpenChange).toHaveBeenCalledWith(false));
    // A successful hook shows nothing on Save.
    expect(screen.queryByTestId("project-settings-entry-post-bind-h1")).not.toBeInTheDocument();
  });

  it("re-runs the post-bind command on the saved path and shows any status", async () => {
    listEntriesMock.mockResolvedValue([entry("h1", "/repo")]);
    getProjectMock.mockResolvedValue({ id: "p_1", name: "Work", config: { host_id: "h1" } });
    putEntryMock.mockResolvedValue({
      ...entry("h1", "/repo"),
      post_bind: { status: "not_configured", exit_code: null, output: null, error: null },
    });
    renderDialog();
    await waitFor(() =>
      expect(screen.getByTestId("project-settings-entry-h1")).toHaveTextContent("/repo"),
    );

    // Touch the draft only — Run must re-PUT the saved path, not the draft.
    fireEvent.click(screen.getByTestId("project-settings-entry-browse-h1"));
    fireEvent.click(screen.getByText("pick dir"));
    fireEvent.click(screen.getByTestId("project-settings-entry-run-post-bind-h1"));

    await waitFor(() =>
      expect(screen.getByTestId("project-settings-entry-post-bind-h1")).toHaveTextContent(
        "post-bind command not_configured",
      ),
    );
    expect(putEntryMock).toHaveBeenCalledWith("p_1", "h1", "/repo");
    // A re-run writes no config.
    expect(updateMock).not.toHaveBeenCalled();
  });

  it("shows only a success line when a re-run of the post-bind command exits 0", async () => {
    listEntriesMock.mockResolvedValue([entry("h1", "/repo")]);
    getProjectMock.mockResolvedValue({ id: "p_1", name: "Work", config: { host_id: "h1" } });
    putEntryMock.mockResolvedValue({
      ...entry("h1", "/repo"),
      post_bind: { status: "ok", exit_code: 0, output: "joined\n", error: null },
    });
    renderDialog();
    await waitFor(() =>
      expect(screen.getByTestId("project-settings-entry-h1")).toHaveTextContent("/repo"),
    );

    fireEvent.click(screen.getByTestId("project-settings-entry-run-post-bind-h1"));

    await waitFor(() =>
      expect(screen.getByTestId("project-settings-entry-post-bind-h1")).toHaveTextContent(
        "Post-bind command succeeded",
      ),
    );
    const outcome = screen.getByTestId("project-settings-entry-post-bind-h1");
    expect(outcome.textContent).toBe("Post-bind command succeeded");
    expect(outcome).not.toHaveTextContent("joined");
    expect(outcome).not.toHaveTextContent("exit code");
  });

  it("invalidates host-roots and collaboration after a partial entry save, keeping drafts", async () => {
    hostsMock.mockReturnValue({ data: [LAPTOP, DESKTOP] });
    listEntriesMock.mockResolvedValue([entry("h1", "/one"), entry("h2", "/two")]);
    getProjectMock.mockResolvedValue({ id: "p_1", name: "Work", config: { host_id: "h1" } });
    const { client } = renderDialog();
    const invalidateSpy = vi.spyOn(client, "invalidateQueries");
    await waitFor(() =>
      expect(screen.getByTestId("project-settings-entry-h1")).toHaveTextContent("/one"),
    );

    fireEvent.click(screen.getByTestId("project-settings-entry-browse-h1"));
    fireEvent.click(screen.getByText("pick dir"));
    // h2's detail pane renders only once its row is selected.
    fireEvent.click(screen.getByTestId("project-settings-entry-h2"));
    fireEvent.click(screen.getByTestId("project-settings-entry-browse-h2"));
    fireEvent.click(screen.getByText("pick dir"));
    // Row h1 commits; row h2's PUT fails — partial progress still refreshes.
    putEntryMock
      .mockResolvedValueOnce(entry("h1", "/picked/dir"))
      .mockRejectedValueOnce(new Error("host is offline"));
    fireEvent.click(screen.getByTestId("project-settings-save"));

    await waitFor(() =>
      expect(screen.getByTestId("project-settings-entry-error-h2")).toHaveTextContent(
        "host is offline",
      ),
    );
    await waitFor(() =>
      expect(invalidateSpy).toHaveBeenCalledWith({
        queryKey: ["project-host-roots", "p_1"],
      }),
    );
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["project-collaboration", "p_1"] });
    // Entries are not refetched (that would reseed the drafts) and no config lands.
    expect(invalidateSpy).not.toHaveBeenCalledWith({ queryKey: ["project-entries", "p_1"] });
    expect(updateMock).not.toHaveBeenCalled();
    // h2's detail keeps its draft; h1's list row keeps its own.
    expect(screen.getByTestId("project-settings-entry-browse-h2")).toHaveTextContent("/picked/dir");
    expect(screen.getByTestId("project-settings-host-path-h1")).toHaveTextContent("/picked/dir");
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

  it("opens a row's directory dialog and commits the confirmed path", async () => {
    listEntriesMock.mockResolvedValue([entry("h1", "/repo")]);
    getProjectMock.mockResolvedValue({ id: "p_1", name: "Work", config: { host_id: "h1" } });
    renderDialog();
    await waitFor(() =>
      expect(screen.getByTestId("project-settings-entry-browse-h1")).toHaveTextContent("/repo"),
    );

    // Open the shared dialog against the row's host; confirming updates the
    // trigger label and closes the dialog.
    fireEvent.click(screen.getByTestId("project-settings-entry-browse-h1"));
    expect(screen.getByTestId("mock-workspace-picker")).toBeInTheDocument();
    expect(workspacePickerPropsMock).toHaveBeenLastCalledWith(
      expect.objectContaining({ hostId: "h1", initialPath: "/repo" }),
    );
    fireEvent.click(screen.getByText("pick dir"));
    expect(screen.getByTestId("project-settings-entry-browse-h1")).toHaveTextContent("/picked/dir");

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
    getProjectMock.mockResolvedValue({
      id: "p_1",
      name: "Work",
      config: { agent_id: "ag_claude", model: "opus" },
    });
    renderDialog();
    await waitFor(() => expect(screen.getByTestId("project-settings-model")).toBeInTheDocument());
    // The stored alias seeds the control (the static Claude vocab labels it).
    await waitFor(() =>
      expect(screen.getByTestId("project-settings-model")).toHaveTextContent(/opus/i),
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

  it("hides the collaboration section when the project_assignments feature is off", async () => {
    getProjectMock.mockResolvedValue({ id: "p_1", name: "Work", config: {} });
    renderDialog();
    await waitFor(() =>
      expect((screen.getByTestId("project-settings-save") as HTMLButtonElement).disabled).toBe(
        false,
      ),
    );

    expect(screen.queryByTestId("project-collaboration-section")).not.toBeInTheDocument();
    expect(screen.queryByTestId("project-collaboration-repo-open")).not.toBeInTheDocument();
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
    const { rerenderOpen } = renderDialog();
    await waitFor(() =>
      expect((screen.getByTestId("project-settings-save") as HTMLButtonElement).disabled).toBe(
        false,
      ),
    );

    fireEvent.click(screen.getByTestId("project-settings-worktree"));
    fireEvent.mouseDown(screen.getByRole("tab", { name: "Collaboration" }), { button: 0 });
    await waitFor(() =>
      expect(screen.getByTestId("project-collaboration-repo-open")).toBeInTheDocument(),
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
    listCatalogsMock.mockResolvedValue([
      catalogRow("h1", "codex", [{ id: "gpt-6-sol", displayName: "GPT-6-Sol" }]),
    ]);
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
    syncCatalogsMock.mockResolvedValue([
      catalogRow("h1", "claude-native", [{ id: "opus", displayName: "Opus" }]),
    ]);
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
    await pickOption("project-settings-host-model-h1", "Opus");
    expect(syncCatalogsMock).toHaveBeenCalledTimes(1);
    expect(syncCatalogsMock).toHaveBeenCalledWith({ hostId: "h1", harness: "claude-native" });
    fireEvent.click(screen.getByTestId("project-settings-host-effort-h1"));
    fireEvent.click(await screen.findByRole("option", { name: "High" }));
    expect(syncCatalogsMock).toHaveBeenCalledTimes(1);

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
    listCatalogsMock.mockResolvedValue([
      catalogRow("h1", "codex", [{ id: "gpt-6-sol", displayName: "GPT-6-Sol" }]),
    ]);
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

  it("deletes a host row and drops both its entry and its calling_defaults key", async () => {
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

    await waitFor(() => expect(deleteEntryMock).toHaveBeenCalledWith("p_1", "h2"));
    expect(updateMock).toHaveBeenCalledWith("p_1", {
      calling_defaults: { h1: { agent_id: "ag_claude" } },
    });
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
