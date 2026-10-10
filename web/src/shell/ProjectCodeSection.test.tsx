import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { ProjectCodeSection } from "./ProjectCodeSection";
import {
  deleteProjectEntry,
  deleteProjectHostBinding,
  deleteProjectRepository,
  getAgentCodeNote,
  getHostFolderFacts,
  getProjectCollaboration,
  getProjectHostRoots,
  listProjectEntries,
  putProjectEntry,
  putProjectHostBinding,
  putProjectRepository,
  verifyProjectHostBinding,
  type HostFolderFacts,
  type HostFolderFactsResult,
  type ProjectCollaboration,
  type ProjectHostBinding,
  type ProjectHostEntry,
  type ProjectRepository,
} from "@/lib/projectsApi";

vi.mock("@/lib/projectsApi", () => ({
  deleteProjectEntry: vi.fn(),
  deleteProjectHostBinding: vi.fn(),
  deleteProjectRepository: vi.fn(),
  getAgentCodeNote: vi.fn(),
  getHostFolderFacts: vi.fn(),
  getProjectCollaboration: vi.fn(),
  getProjectHostRoots: vi.fn(),
  listProjectEntries: vi.fn(),
  putProjectEntry: vi.fn(),
  putProjectHostBinding: vi.fn(),
  putProjectRepository: vi.fn(),
  verifyProjectHostBinding: vi.fn(),
}));
vi.mock("./WorkspacePicker", () => ({
  isNavigablePath: (path: string) => path.startsWith("/"),
  HostWorkspacePicker: ({ onSelect }: { onSelect: (path: string) => void }) => (
    <div>
      <button type="button" onClick={() => onSelect("/opt/work/omnigent/fork/wt")}>
        Choose folder
      </button>
    </div>
  ),
}));
const hostsMock = vi.hoisted(() => ({
  data: [] as {
    host_id: string;
    name: string;
    owner: string;
    status: string;
    platform?: string;
    sandbox_provider?: string;
  }[],
}));
vi.mock("@/hooks/useHosts", () => ({
  useHosts: () => ({ data: hostsMock.data }),
}));

const getMock = vi.mocked(getProjectCollaboration);
const listEntriesMock = vi.mocked(listProjectEntries);
const getHostRootsMock = vi.mocked(getProjectHostRoots);
const getFactsMock = vi.mocked(getHostFolderFacts);
const getAgentNoteMock = vi.mocked(getAgentCodeNote);
const putRepoMock = vi.mocked(putProjectRepository);
const putBindingMock = vi.mocked(putProjectHostBinding);
const putEntryMock = vi.mocked(putProjectEntry);

function collaboration(overrides: Partial<ProjectCollaboration> = {}): ProjectCollaboration {
  return {
    repositories: [],
    bindings: [],
    problems: [],
    setup_outcomes: [],
    ...overrides,
  };
}

function repo(overrides: Partial<ProjectRepository> = {}): ProjectRepository {
  return {
    id: "r_web",
    project_id: "p_1",
    name: "web",
    role: "code",
    remote_url: "https://git.example.test/acme/web.git",
    default_branch: "main",
    context_manifest_path: ".agents/project/manifest.json",
    revision: 1,
    created_at: 1,
    updated_at: null,
    ...overrides,
  };
}

function binding(overrides: Partial<ProjectHostBinding> = {}): ProjectHostBinding {
  return {
    id: "b_web",
    project_id: "p_1",
    host_id: "h1",
    name: "web",
    is_primary: true,
    repository_id: "r_web",
    workspace: "/opt/work/omnigent/fork/web",
    enabled: true,
    revision: 1,
    path_verified_at: 1,
    created_at: 1,
    updated_at: null,
    ...overrides,
  };
}

function entry(hostId: string, workspace: string): ProjectHostEntry {
  return { host_id: hostId, workspace, updated_at: null };
}

function okFacts(overrides: Partial<HostFolderFacts> = {}): HostFolderFacts {
  return {
    exists: true,
    is_dir: true,
    is_repo: true,
    toplevel: "/opt/work/omnigent/fork/web",
    branch: "main",
    default_branch: "main",
    head: "abc1234def5678901234",
    detached: false,
    dirty: false,
    remotes: [{ name: "origin", url: "https://git.example.test/acme/web.git" }],
    setup_command_configured: false,
    error: null,
    ...overrides,
  };
}

function renderSection() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <ProjectCodeSection projectId="p_1" />
    </QueryClientProvider>,
  );
}

async function openAddForm() {
  await waitFor(() => expect(screen.getByTestId("project-code-add-repo-open")).toBeInTheDocument());
  fireEvent.click(screen.getByTestId("project-code-add-repo-open"));
}

async function chooseFolder() {
  fireEvent.click(screen.getByTestId("project-code-add-browse"));
  fireEvent.click(screen.getByText("Choose folder"));
}

beforeEach(() => {
  hostsMock.data = [
    { host_id: "h1", name: "Laptop", owner: "me", status: "online", platform: "macOS" },
    { host_id: "h2", name: "Desktop", owner: "me", status: "online" },
    { host_id: "h3", name: "Tower", owner: "me", status: "online" },
    {
      host_id: "sandbox-1",
      name: "Sandbox",
      owner: "me",
      status: "online",
      sandbox_provider: "modal",
    },
  ];
  getMock.mockReset();
  listEntriesMock.mockReset();
  getHostRootsMock.mockReset();
  getFactsMock.mockReset();
  getAgentNoteMock.mockReset();
  putRepoMock.mockReset();
  putBindingMock.mockReset();
  putEntryMock.mockReset();
  vi.mocked(deleteProjectRepository).mockReset();
  vi.mocked(deleteProjectHostBinding).mockReset();
  vi.mocked(deleteProjectEntry).mockReset();
  vi.mocked(verifyProjectHostBinding).mockReset();

  getMock.mockResolvedValue(collaboration());
  listEntriesMock.mockResolvedValue([]);
  getHostRootsMock.mockResolvedValue({
    roots: [],
    default_host_id: null,
    default_host_reason: "none",
  });
  getFactsMock.mockResolvedValue({ state: "ok", facts: okFacts() });
  getAgentNoteMock.mockResolvedValue({ text: "saved preview", delivered: true, reason: null });
});

afterEach(cleanup);

describe("ProjectCodeSection", () => {
  it("offers a separate offline checkout only after choosing another folder", async () => {
    hostsMock.data = hostsMock.data.map((host) => ({ ...host, status: "offline" }));
    getMock.mockResolvedValue(collaboration({ repositories: [repo()], bindings: [binding()] }));
    listEntriesMock.mockResolvedValue([entry("h1", "/opt/work/omnigent/fork/web")]);
    getFactsMock.mockResolvedValue({ state: "offline" });
    putBindingMock.mockResolvedValue(binding());
    renderSection();
    const choose = await screen.findByTestId("project-code-binding-browse-h1-web");
    expect(screen.queryByTestId("project-code-binding-path-h1-web")).not.toBeInTheDocument();
    fireEvent.click(choose);
    const folder = screen.getByTestId("project-code-binding-path-h1-web");
    fireEvent.change(folder, { target: { value: "/opt/work/omnigent/fork/other" } });
    fireEvent.click(screen.getByTestId("project-code-binding-save-h1-web"));
    await waitFor(() =>
      expect(putBindingMock).toHaveBeenCalledWith("p_1", "h1", "web", {
        workspace: "/opt/work/omnigent/fork/other",
        repository_name: "web",
      }),
    );
  });
  it("moves the code mark with one role update", async () => {
    const web = repo();
    const api = repo({
      id: "r_api",
      name: "api",
      role: "related",
      remote_url: "https://git.example.test/acme/api.git",
    });
    getMock.mockResolvedValue(collaboration({ repositories: [web, api] }));
    putRepoMock.mockResolvedValue({ ...api, role: "code" });
    renderSection();

    await screen.findByTestId("project-code-repo-role-api");
    fireEvent.change(screen.getByTestId("project-code-repo-role-api"), {
      target: { value: "code" },
    });

    await waitFor(() => expect(putRepoMock).toHaveBeenCalledTimes(1));
    expect(putRepoMock).toHaveBeenCalledWith("p_1", "api", {
      remote_url: "https://git.example.test/acme/api.git",
      default_branch: "main",
      context_manifest_path: ".agents/project/manifest.json",
      role: "code",
    });
  });

  it("adds a repository from a folder, then binds it to the host", async () => {
    getMock.mockResolvedValue(collaboration());
    getFactsMock.mockResolvedValue({
      state: "ok",
      facts: okFacts({
        branch: "feature/code",
        remotes: [{ name: "origin", url: "https://git.example.test/acme/web.git" }],
      }),
    });
    putRepoMock.mockResolvedValue(repo());
    putBindingMock.mockResolvedValue(binding({ checked: true }));
    renderSection();

    await openAddForm();
    await chooseFolder();

    await waitFor(() => expect(screen.getByTestId("project-code-add-name")).toHaveValue("web"));
    expect(screen.getByTestId("project-code-add-branch")).toHaveValue("main");
    fireEvent.click(screen.getByTestId("project-code-add-submit"));

    await waitFor(() => expect(putBindingMock).toHaveBeenCalled());
    expect(putRepoMock).toHaveBeenCalledWith("p_1", "web", {
      remote_url: "https://git.example.test/acme/web.git",
      default_branch: "main",
      role: "code",
    });
    expect(putBindingMock).toHaveBeenCalledWith("p_1", "h1", "web", {
      workspace: "/opt/work/omnigent/fork/wt",
      repository_name: "web",
    });
    expect(putRepoMock.mock.invocationCallOrder[0]).toBeLessThan(
      putBindingMock.mock.invocationCallOrder[0]!,
    );
  });

  it("offers a typed address only while no folder is chosen, and creates no binding", async () => {
    getMock.mockResolvedValue(collaboration());
    putRepoMock.mockResolvedValue(repo());
    renderSection();

    await openAddForm();
    expect(screen.getByTestId("project-code-add-by-address")).toBeInTheDocument();
    expect(screen.queryByTestId("project-code-add-url")).not.toBeInTheDocument();

    fireEvent.click(screen.getByTestId("project-code-add-by-address"));
    expect(screen.getByTestId("project-code-add-url")).toBeInTheDocument();
    fireEvent.change(screen.getByTestId("project-code-add-url"), {
      target: { value: "https://git.example.test/team/repo.git" },
    });
    fireEvent.change(screen.getByTestId("project-code-add-name"), {
      target: { value: "team-repo" },
    });
    fireEvent.change(screen.getByTestId("project-code-add-branch"), {
      target: { value: "main" },
    });
    fireEvent.click(screen.getByTestId("project-code-add-submit"));

    await waitFor(() => expect(putRepoMock).toHaveBeenCalledTimes(1));
    expect(putRepoMock).toHaveBeenCalledWith("p_1", "team-repo", {
      remote_url: "https://git.example.test/team/repo.git",
      default_branch: "main",
      role: "code",
    });
    expect(putBindingMock).not.toHaveBeenCalled();

    // A chosen folder hides the typed-address path.
    await openAddForm();
    await chooseFolder();
    await waitFor(() =>
      expect(screen.getByTestId("project-code-add-folder")).toHaveValue(
        "/opt/work/omnigent/fork/wt",
      ),
    );
    expect(screen.queryByTestId("project-code-add-by-address")).not.toBeInTheDocument();
  });

  it("keeps stored folders when the host facts are offline", async () => {
    getMock.mockResolvedValue(collaboration({ repositories: [repo()], bindings: [binding()] }));
    listEntriesMock.mockResolvedValue([entry("h1", "/opt/work/omnigent")]);
    getFactsMock.mockResolvedValue({ state: "offline" });
    renderSection();

    await waitFor(() => expect(screen.getAllByText("host offline")).toHaveLength(2));
    expect(screen.getByText("/opt/work/omnigent/fork/web")).toBeInTheDocument();
    expect(screen.getByText("/opt/work/omnigent")).toBeInTheDocument();
  });

  it("shows the no-code note until a code repository exists", async () => {
    getMock.mockResolvedValue(collaboration({ repositories: [repo({ role: "related" })] }));
    renderSection();

    await screen.findByTestId("project-code-no-code-repo");
    expect(screen.getByTestId("project-code-no-code-repo")).toHaveTextContent(
      "No code repository yet",
    );

    cleanup();
    getMock.mockResolvedValue(collaboration({ repositories: [repo({ role: "code" })] }));
    renderSection();
    await screen.findByTestId("project-code-repo-web");
    expect(screen.queryByTestId("project-code-no-code-repo")).not.toBeInTheDocument();
  });

  it("keeps entry folders read-only on the details page", async () => {
    listEntriesMock.mockResolvedValue([entry("h1", "/opt/work/omnigent/fork/wt")]);
    renderSection();
    await screen.findByTestId("project-code-entry-h1");
    expect(screen.queryByTestId("project-code-entry-browse-h1")).not.toBeInTheDocument();
    expect(screen.queryByTestId("project-code-entry-path-h1")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "About the entry folder on Laptop" }));
    expect(screen.getByText(/Set or change it on the Session defaults page/)).toBeInTheDocument();
    expect(putEntryMock).not.toHaveBeenCalled();
  });

  it("marks a repository folder that equals the project folder", async () => {
    getMock.mockResolvedValue(collaboration({ repositories: [repo()], bindings: [binding()] }));
    listEntriesMock.mockResolvedValue([entry("h1", "/opt/work/omnigent/fork/web/")]);
    renderSection();

    await screen.findByTestId("project-code-binding-same-h1-web");
    expect(screen.getByTestId("project-code-binding-same-h1-web")).toHaveTextContent(
      "Same as entry folder",
    );
  });

  it("renders each folder facts status", async () => {
    getMock.mockResolvedValue(
      collaboration({
        repositories: [repo()],
        bindings: [
          binding(),
          binding({ id: "b_web_2", host_id: "h2", workspace: "/opt/work/omnigent/fork/api" }),
        ],
      }),
    );
    listEntriesMock.mockResolvedValue([
      entry("h1", "/opt/work/omnigent"),
      entry("h2", "/opt/work/omnigent/clean"),
      entry("h3", "/opt/work/omnigent/other"),
    ]);
    getFactsMock.mockImplementation(
      async (hostId: string, path: string): Promise<HostFolderFactsResult> => {
        if (hostId === "h1" && path === "/opt/work/omnigent") {
          return {
            state: "ok",
            facts: okFacts({
              exists: false,
              is_dir: false,
              is_repo: false,
              branch: null,
              head: null,
            }),
          };
        }
        if (hostId === "h1") {
          return { state: "ok", facts: okFacts({ is_repo: false, branch: null, head: null }) };
        }
        if (hostId === "h2" && path === "/opt/work/omnigent/clean") return { state: "offline" };
        if (hostId === "h2") return { state: "unsupported" };
        return {
          state: "ok",
          facts: okFacts({ branch: "dev", head: "fedcba9876543210", dirty: true }),
        };
      },
    );
    renderSection();

    await waitFor(() =>
      expect(screen.getByTestId("project-code-entry-facts-h1")).toHaveTextContent("folder missing"),
    );
    expect(screen.getByTestId("project-code-binding-facts-h1-web")).toHaveTextContent(
      "not a git repository",
    );
    expect(screen.getByTestId("project-code-entry-facts-h2")).toHaveTextContent("host offline");
    expect(screen.getByTestId("project-code-binding-facts-h2-web")).toHaveTextContent(
      "host needs an update",
    );
    const dirty = screen.getByTestId("project-code-entry-facts-h3");
    expect(dirty).toHaveTextContent("dev");
    expect(dirty).toHaveTextContent("HEAD fedcba9");
    expect(dirty).toHaveTextContent("uncommitted changes");
  });

  it("warns when no folder remote URL matches the repository URL", async () => {
    getMock.mockResolvedValue(collaboration({ repositories: [repo()], bindings: [binding()] }));
    getFactsMock.mockResolvedValue({
      state: "ok",
      facts: okFacts({
        remotes: [{ name: "origin", url: "https://git.example.test/other/web.git" }],
      }),
    });
    renderSection();

    await screen.findByTestId("project-code-binding-facts-h1-web-remote-mismatch");
    expect(
      screen.getByTestId("project-code-binding-facts-h1-web-remote-mismatch"),
    ).toHaveTextContent("None of this folder's remote URLs matches the repository URL.");

    cleanup();
    getFactsMock.mockResolvedValue({ state: "ok", facts: okFacts() });
    renderSection();
    await screen.findByTestId("project-code-binding-facts-h1-web");
    expect(
      screen.queryByTestId("project-code-binding-facts-h1-web-remote-mismatch"),
    ).not.toBeInTheDocument();
  });

  it("shows the host-roots lines, labelling a legacy root, and the refused label only without one", async () => {
    getMock.mockResolvedValue(collaboration({ repositories: [repo()], bindings: [binding()] }));
    listEntriesMock.mockResolvedValue([
      entry("h1", "/opt/work/omnigent"),
      entry("h2", "/opt/work/omnigent/other"),
    ]);
    getHostRootsMock.mockResolvedValue({
      roots: [
        {
          host_id: "h1",
          workspace: "/opt/work/omnigent",
          source: "entry",
          checkout: "/opt/work/omnigent/fork/web",
        },
        {
          host_id: "h3",
          workspace: "/opt/work/omnigent/legacy",
          source: "config",
          checkout: null,
        },
      ],
      default_host_id: "h1",
      default_host_reason: "single_root",
    });
    renderSection();

    fireEvent.click(await screen.findByRole("button", { name: "About session folders on Laptop" }));
    await screen.findByTestId("project-code-root-h1");
    expect(screen.getByTestId("project-code-root-h1")).toHaveTextContent(
      "New sessions open in: /opt/work/omnigent",
    );
    expect(screen.getByTestId("project-code-root-h1")).toHaveTextContent(
      "New worktrees come from: /opt/work/omnigent/fork/web",
    );
    expect(screen.queryByTestId("project-code-refused-h1")).not.toBeInTheDocument();

    await screen.findByTestId("project-code-refused-h2");
    expect(screen.getByTestId("project-code-refused-h2")).toHaveTextContent(
      "Sessions here are refused — set a project folder",
    );
    expect(screen.queryByTestId("project-code-root-h2")).not.toBeInTheDocument();

    fireEvent.keyDown(screen.getByTestId("project-code-root-h1"), {
      key: "Escape",
      code: "Escape",
    });
    fireEvent.click(screen.getByRole("button", { name: "About session folders on Tower" }));
    await screen.findByTestId("project-code-root-h3");
    expect(screen.getByTestId("project-code-root-h3")).toHaveTextContent(
      "New sessions open in: /opt/work/omnigent/legacy (from the project's single-folder setting)",
    );
    expect(screen.getByTestId("project-code-root-h3")).toHaveTextContent(
      "New worktrees come from: /opt/work/omnigent/legacy",
    );
  });

  it("shows the setup block only when the host reports a setup command, with the outcome time", async () => {
    getMock.mockResolvedValue(
      collaboration({
        repositories: [repo()],
        bindings: [binding()],
        setup_outcomes: [
          {
            host_id: "h1",
            kind: "binding",
            target: "web",
            status: "ok",
            exit_code: 0,
            output: "installed",
            error: null,
            at: "2026-10-09T12:34:56+00:00",
          },
        ],
      }),
    );
    listEntriesMock.mockResolvedValue([entry("h1", "/opt/work/omnigent")]);
    getFactsMock.mockImplementation(async (_hostId: string, path: string) => ({
      state: "ok",
      facts: okFacts({ setup_command_configured: path === "/opt/work/omnigent/fork/web" }),
    }));
    renderSection();

    await screen.findByTestId("project-code-binding-facts-h1-web-setup");
    expect(screen.getByTestId("project-code-binding-facts-h1-web-setup")).toHaveTextContent(
      "Configured on host",
    );
    const outcome = screen.getByTestId("project-code-binding-facts-h1-web-setup-outcome");
    expect(outcome).toHaveTextContent("Last run: ok");
    expect(outcome).toHaveTextContent("2026-10-09 12:34 UTC");
    expect(screen.getByTestId("project-code-binding-facts-h1-web-run-again")).toBeInTheDocument();
    expect(screen.getByTestId("project-code-entry-facts-h1-setup")).toHaveTextContent(
      "Not configured",
    );
  });

  it("shows the agent preview with the delivered notes", async () => {
    getMock.mockResolvedValue(collaboration({ repositories: [repo()], bindings: [binding()] }));
    getAgentNoteMock.mockResolvedValue({
      text: "This project's code on this host:\n- web (the code you change): /opt/work/omnigent/fork/web",
      delivered: false,
      reason: "host_update_needed",
    });
    renderSection();

    fireEvent.click(await screen.findByRole("button", { name: "What agents receive on h1" }));
    await screen.findByTestId("project-code-agent-note-update-h1");
    expect(screen.getByTestId("project-code-agent-note-update-h1")).toHaveTextContent(
      "This host needs an update before agents receive this",
    );
    expect(
      screen.getByTestId("project-code-agent-note-update-h1").parentElement!,
    ).toHaveTextContent("This project's code on this host:");
    expect(screen.getByText("OpenCode sessions do not receive this yet")).toBeInTheDocument();

    cleanup();
    getAgentNoteMock.mockResolvedValue({
      text: "saved preview",
      delivered: false,
      reason: "host_offline",
    });
    renderSection();
    fireEvent.click(await screen.findByRole("button", { name: "What agents receive on h1" }));
    await screen.findByTestId("project-code-agent-note-offline-h1");
    expect(screen.getByTestId("project-code-agent-note-offline-h1")).toHaveTextContent(
      "Host offline — shown from saved settings",
    );
  });

  it("keeps one picker when moving from a repository binding to adding a repository", async () => {
    getMock.mockResolvedValue(collaboration({ repositories: [repo()], bindings: [binding()] }));
    putBindingMock.mockResolvedValue(binding());
    renderSection();
    fireEvent.click(await screen.findByTestId("project-code-binding-browse-h1-web"));
    expect(screen.getAllByText("Choose folder")).toHaveLength(1);
    fireEvent.click(screen.getByText("Choose folder"));
    await waitFor(() => expect(putBindingMock).toHaveBeenCalled());
    await openAddForm();
    await chooseFolder();
    expect(screen.getByTestId("project-code-add-folder")).toHaveValue("/opt/work/omnigent/fork/wt");
    expect(putEntryMock).not.toHaveBeenCalled();
  });

  it("edits a legacy binding under its own name while keeping its repository", async () => {
    const primary = binding({ id: "b_primary", name: "primary" });
    getMock.mockResolvedValue(collaboration({ repositories: [repo()], bindings: [primary] }));
    putBindingMock.mockResolvedValue({
      ...primary,
      workspace: "/opt/work/omnigent/fork/wt",
    });
    renderSection();

    await screen.findByTestId("project-code-binding-browse-h1-web");
    fireEvent.click(screen.getByTestId("project-code-binding-browse-h1-web"));
    fireEvent.click(screen.getByText("Choose folder"));

    await waitFor(() =>
      expect(putBindingMock).toHaveBeenCalledWith("p_1", "h1", "primary", {
        workspace: "/opt/work/omnigent/fork/wt",
        repository_name: "web",
      }),
    );
  });

  it("rejects adding a repository whose name already exists, with no PUT", async () => {
    getMock.mockResolvedValue(collaboration({ repositories: [repo()] }));
    renderSection();

    await openAddForm();
    fireEvent.click(screen.getByTestId("project-code-add-by-address"));
    fireEvent.change(screen.getByTestId("project-code-add-url"), {
      target: { value: "https://git.example.test/acme/web.git" },
    });
    fireEvent.change(screen.getByTestId("project-code-add-name"), {
      target: { value: "web" },
    });
    fireEvent.click(screen.getByTestId("project-code-add-submit"));

    fireEvent.change(screen.getByTestId("project-code-add-branch"), { target: { value: "main" } });
    fireEvent.click(screen.getByTestId("project-code-add-submit"));
    expect(await screen.findByTestId("project-code-add-error")).toHaveTextContent(
      "A repository named web already exists",
    );
    expect(putRepoMock).not.toHaveBeenCalled();
  });

  it("refreshes collaboration and shows the error when the binding PUT fails after the repository", async () => {
    getMock.mockResolvedValue(collaboration());
    putRepoMock.mockResolvedValue(repo());
    putBindingMock.mockRejectedValue(new Error("binding failed"));
    renderSection();

    await openAddForm();
    await chooseFolder();
    await waitFor(() => expect(screen.getByTestId("project-code-add-name")).toHaveValue("web"));

    fireEvent.click(screen.getByTestId("project-code-add-submit"));

    await waitFor(() =>
      expect(screen.getByTestId("project-code-error")).toHaveTextContent("binding failed"),
    );
    expect(putRepoMock).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(getMock.mock.calls.length).toBeGreaterThan(1));
  });

  it("does not let a late facts response for an old path fill the form", async () => {
    getMock.mockResolvedValue(collaboration());
    let resolveFirst!: (result: HostFolderFactsResult) => void;
    let resolveSecond!: (result: HostFolderFactsResult) => void;
    getFactsMock.mockImplementation(
      (_hostId: string, path: string) =>
        new Promise<HostFolderFactsResult>((resolve) => {
          if (path === "/opt/work/first") resolveFirst = resolve;
          else resolveSecond = resolve;
        }),
    );
    renderSection();

    await openAddForm();
    const folderInput = screen.getByTestId("project-code-add-folder");
    fireEvent.change(folderInput, { target: { value: "/opt/work/first" } });
    fireEvent.blur(folderInput);
    fireEvent.change(folderInput, { target: { value: "/opt/work/second" } });
    fireEvent.blur(folderInput);
    await waitFor(() => expect(getFactsMock).toHaveBeenCalledTimes(2));

    await act(async () => {
      resolveSecond({
        state: "ok",
        facts: okFacts({
          branch: "feature/second",
          default_branch: "second",
          remotes: [{ name: "origin", url: "https://git.example.test/acme/second.git" }],
        }),
      });
    });
    await waitFor(() => expect(screen.getByTestId("project-code-add-name")).toHaveValue("second"));
    expect(screen.getByTestId("project-code-add-branch")).toHaveValue("second");

    await act(async () => {
      resolveFirst({
        state: "ok",
        facts: okFacts({
          branch: "feature/first",
          default_branch: "first",
          remotes: [{ name: "origin", url: "https://git.example.test/acme/first.git" }],
        }),
      });
    });
    expect(screen.getByTestId("project-code-add-name")).toHaveValue("second");
    expect(screen.getByTestId("project-code-add-branch")).toHaveValue("second");
  });

  it("shows a message when reading a committed folder fails", async () => {
    getMock.mockResolvedValue(collaboration());
    getFactsMock.mockRejectedValue(new Error("host unreachable"));
    renderSection();

    await openAddForm();
    const folderInput = screen.getByTestId("project-code-add-folder");
    fireEvent.change(folderInput, { target: { value: "/opt/work/omnigent/fork/web" } });
    fireEvent.blur(folderInput);

    expect(await screen.findByText("Couldn't read this folder.")).toBeInTheDocument();
  });

  it("loads facts for a typed path when it is committed", async () => {
    getMock.mockResolvedValue(collaboration());
    getFactsMock.mockResolvedValue({
      state: "ok",
      facts: okFacts({
        branch: "feature/settings",
        default_branch: "release",
        remotes: [{ name: "upstream", url: "https://git.example.test/acme/tools.git" }],
      }),
    });
    renderSection();

    await openAddForm();
    const folderInput = screen.getByTestId("project-code-add-folder");
    fireEvent.change(folderInput, { target: { value: "/opt/work/omnigent/tools" } });
    expect(getFactsMock).not.toHaveBeenCalled();
    fireEvent.blur(folderInput);

    await waitFor(() =>
      expect(getFactsMock).toHaveBeenCalledWith("h1", "/opt/work/omnigent/tools"),
    );
    await waitFor(() => expect(screen.getByTestId("project-code-add-name")).toHaveValue("tools"));
    expect(screen.getByTestId("project-code-add-branch")).toHaveValue("release");
  });

  it("saves typed folders for an offline host and shows the unchecked label", async () => {
    hostsMock.data = hostsMock.data.map((host) =>
      host.host_id === "h1" ? { ...host, status: "offline" } : host,
    );
    getMock.mockResolvedValue(collaboration({ repositories: [repo()], bindings: [binding()] }));
    listEntriesMock.mockResolvedValue([entry("h1", "/opt/work/omnigent/old")]);
    getFactsMock.mockResolvedValue({ state: "offline" });
    putEntryMock.mockResolvedValue({ ...entry("h1", "/opt/work/omnigent/new"), checked: false });
    putBindingMock.mockResolvedValue(binding({ checked: false }));
    renderSection();

    await screen.findByTestId("project-code-entry-h1");
    expect(screen.queryByTestId("project-code-entry-path-h1")).not.toBeInTheDocument();

    const bindingInput = screen.getByTestId("project-code-binding-path-h1-web");
    expect(screen.getByTestId("project-code-binding-browse-h1-web")).toBeDisabled();
    fireEvent.change(bindingInput, { target: { value: "/opt/work/omnigent/new-repo" } });
    fireEvent.click(screen.getByTestId("project-code-binding-save-h1-web"));
    await waitFor(() =>
      expect(putBindingMock).toHaveBeenCalledWith("p_1", "h1", "web", {
        workspace: "/opt/work/omnigent/new-repo",
        repository_name: "web",
      }),
    );
    await screen.findByTestId("project-code-unchecked-h1-web");
    expect(screen.getByTestId("project-code-unchecked-h1-web")).toHaveTextContent(
      "Saved without checking — host offline",
    );
  });

  it("shows a dangling binding with a remove action", async () => {
    const orphan = binding({
      id: "b_old",
      name: "old",
      repository_id: "r_gone",
      workspace: "/opt/work/omnigent/old",
    });
    getMock.mockResolvedValue(
      collaboration({
        repositories: [repo()],
        bindings: [binding(), orphan],
        problems: [
          {
            code: "dangling_repository",
            binding_id: "b_old",
            host_id: "h1",
            repository_id: "r_gone",
          },
        ],
      }),
    );
    renderSection();

    await screen.findByTestId("project-code-dangling-h1-b_old");
    expect(screen.getByTestId("project-code-dangling-h1-b_old")).toHaveTextContent(
      "This folder points at a repository that no longer exists.",
    );

    fireEvent.click(screen.getByTestId("project-code-dangling-remove-h1-old"));
    await waitFor(() => expect(deleteProjectHostBinding).toHaveBeenCalledWith("p_1", "h1", "old"));
  });

  it("keeps an entry outcome apart from a binding named entry", async () => {
    getMock.mockResolvedValue(
      collaboration({
        repositories: [repo()],
        bindings: [binding({ name: "entry" })],
        setup_outcomes: [
          {
            host_id: "h1",
            kind: "entry",
            target: null,
            status: "ok",
            exit_code: 0,
            output: null,
            error: null,
            at: "2026-10-09T12:34:56+00:00",
          },
          {
            host_id: "h1",
            kind: "binding",
            target: "entry",
            status: "failed",
            exit_code: 2,
            output: null,
            error: null,
            at: "2026-10-09T13:00:00+00:00",
          },
        ],
      }),
    );
    listEntriesMock.mockResolvedValue([entry("h1", "/opt/work/omnigent")]);
    getFactsMock.mockResolvedValue({
      state: "ok",
      facts: okFacts({ setup_command_configured: true }),
    });
    renderSection();

    await screen.findByTestId("project-code-entry-facts-h1-setup-outcome");
    expect(screen.getByTestId("project-code-entry-facts-h1-setup-outcome")).toHaveTextContent(
      "Last run: ok",
    );
    expect(screen.getByTestId("project-code-binding-facts-h1-web-setup-outcome")).toHaveTextContent(
      "Last run: failed",
    );
  });

  it("keeps the chosen remote and branch after the first name edit", async () => {
    getMock.mockResolvedValue(collaboration());
    getFactsMock.mockResolvedValue({
      state: "ok",
      facts: okFacts({
        branch: "main",
        remotes: [
          { name: "origin", url: "https://git.example.test/acme/web.git" },
          { name: "upstream", url: "https://git.example.test/upstream/web.git" },
        ],
      }),
    });
    renderSection();

    await openAddForm();
    await chooseFolder();
    await waitFor(() => expect(screen.getByTestId("project-code-add-name")).toHaveValue("web"));

    fireEvent.change(screen.getByTestId("project-code-add-remote"), {
      target: { value: "upstream" },
    });
    fireEvent.change(screen.getByTestId("project-code-add-branch"), {
      target: { value: "release" },
    });
    fireEvent.change(screen.getByTestId("project-code-add-name"), {
      target: { value: "my-web" },
    });

    expect(screen.getByTestId("project-code-add-remote")).toHaveValue("upstream");
    expect(screen.getByTestId("project-code-add-branch")).toHaveValue("release");
    expect(screen.getByTestId("project-code-add-name")).toHaveValue("my-web");
  });

  it("keeps edited fields when the same folder's facts change on refetch", async () => {
    getMock.mockResolvedValue(collaboration());
    let resolveRefetch!: (result: HostFolderFactsResult) => void;
    getFactsMock.mockResolvedValueOnce({ state: "ok", facts: okFacts() }).mockImplementationOnce(
      () =>
        new Promise<HostFolderFactsResult>((resolve) => {
          resolveRefetch = resolve;
        }),
    );
    renderSection();

    await openAddForm();
    await chooseFolder();
    await waitFor(() => expect(screen.getByTestId("project-code-add-name")).toHaveValue("web"));
    fireEvent.change(screen.getByTestId("project-code-add-branch"), {
      target: { value: "release" },
    });
    fireEvent.change(screen.getByTestId("project-code-add-name"), {
      target: { value: "my-web" },
    });

    fireEvent.click(screen.getByTestId("project-code-refresh"));
    await waitFor(() => expect(getFactsMock).toHaveBeenCalledTimes(2));

    await act(async () => {
      resolveRefetch({
        state: "ok",
        facts: okFacts({
          branch: "changed",
          remotes: [{ name: "upstream", url: "https://git.example.test/acme/changed.git" }],
        }),
      });
    });

    expect(screen.getByTestId("project-code-add-remote")).toHaveValue("origin");
    expect(screen.getByTestId("project-code-add-branch")).toHaveValue("release");
    expect(screen.getByTestId("project-code-add-name")).toHaveValue("my-web");
  });

  it("refills the defaults when a different path is committed", async () => {
    getMock.mockResolvedValue(collaboration());
    getFactsMock.mockImplementation(async (_hostId: string, path: string) => ({
      state: "ok",
      facts:
        path === "/opt/work/first"
          ? okFacts({
              branch: "feature/first",
              default_branch: "first",
              remotes: [{ name: "origin", url: "https://git.example.test/acme/first.git" }],
            })
          : okFacts({
              branch: "feature/second",
              default_branch: "second",
              remotes: [{ name: "upstream", url: "https://git.example.test/acme/second.git" }],
            }),
    }));
    renderSection();

    await openAddForm();
    const folderInput = screen.getByTestId("project-code-add-folder");
    fireEvent.change(folderInput, { target: { value: "/opt/work/first" } });
    fireEvent.blur(folderInput);
    await waitFor(() => expect(screen.getByTestId("project-code-add-name")).toHaveValue("first"));
    expect(screen.getByTestId("project-code-add-branch")).toHaveValue("first");

    fireEvent.change(folderInput, { target: { value: "/opt/work/second" } });
    fireEvent.blur(folderInput);
    await waitFor(() => expect(screen.getByTestId("project-code-add-name")).toHaveValue("second"));
    expect(screen.getByTestId("project-code-add-remote")).toHaveValue("upstream");
    expect(screen.getByTestId("project-code-add-branch")).toHaveValue("second");
  });

  it("keeps repository explanations behind keyboard-accessible help", async () => {
    getMock.mockResolvedValue(collaboration({ repositories: [repo()], bindings: [binding()] }));
    renderSection();
    const trigger = await screen.findByRole("button", { name: "About the role of web" });
    expect(screen.queryByText(/Code we change: on hosts/)).not.toBeInTheDocument();
    act(() => trigger.focus());
    expect(screen.getByText(/Code we change: on hosts/)).toBeInTheDocument();
    expect(trigger).toHaveFocus();
  });

  it("says a related repository's default branch is unused", async () => {
    getMock.mockResolvedValue(collaboration({ repositories: [repo({ role: "related" })] }));
    renderSection();

    await screen.findByTestId("project-code-repo-web");
    fireEvent.click(screen.getByRole("button", { name: "About the default branch of web" }));
    expect(screen.getByText("Kept for reference; sessions do not use it.")).toBeInTheDocument();
  });

  it("requires an explicit main branch when the host cannot report one", async () => {
    getFactsMock.mockResolvedValue({
      state: "ok",
      facts: okFacts({ branch: "feature/settings", default_branch: null }),
    });
    renderSection();
    await openAddForm();
    await chooseFolder();
    const branch = await screen.findByTestId("project-code-add-branch");
    expect(branch).toHaveValue("");
    expect(screen.getByTestId("project-code-add-submit")).toBeDisabled();
    fireEvent.change(branch, { target: { value: "trunk" } });
    expect(screen.getByTestId("project-code-add-submit")).toBeEnabled();
  });

  it("shows a repository folder that equals the project folder only once", async () => {
    getMock.mockResolvedValue(collaboration({ repositories: [repo()], bindings: [binding()] }));
    listEntriesMock.mockResolvedValue([entry("h1", "/opt/work/omnigent/fork/web")]);
    renderSection();

    await screen.findByTestId("project-code-binding-same-h1-web");
    expect(screen.getByTestId("project-code-binding-same-h1-web")).toHaveTextContent(
      "Same as entry folder",
    );
    expect(screen.getAllByText("/opt/work/omnigent/fork/web")).toHaveLength(1);
  });

  it("lets a folder with several remotes pick which one to use", async () => {
    getMock.mockResolvedValue(collaboration({ repositories: [repo()], bindings: [binding()] }));
    getFactsMock.mockResolvedValue({
      state: "ok",
      facts: okFacts({
        remotes: [
          { name: "origin", url: "https://git.example.test/acme/web.git" },
          { name: "upstream", url: "https://git.example.test/upstream/web.git" },
        ],
      }),
    });
    putRepoMock.mockResolvedValue(repo());
    renderSection();

    const select = await screen.findByTestId("project-code-binding-facts-h1-web-remote-select");
    fireEvent.change(select, { target: { value: "upstream" } });
    fireEvent.click(screen.getByTestId("project-code-binding-facts-h1-web-use-remote"));

    await waitFor(() =>
      expect(putRepoMock).toHaveBeenCalledWith("p_1", "web", {
        remote_url: "https://git.example.test/upstream/web.git",
        default_branch: "main",
        context_manifest_path: ".agents/project/manifest.json",
      }),
    );
  });

  it("disables Add and shows the folder status when the folder is missing", async () => {
    getMock.mockResolvedValue(collaboration());
    getFactsMock.mockResolvedValue({
      state: "ok",
      facts: okFacts({ exists: false, is_dir: false, is_repo: false, branch: null, head: null }),
    });
    renderSection();

    await openAddForm();
    await chooseFolder();
    await screen.findByText("folder missing");
    expect(screen.getByTestId("project-code-add-submit")).toBeDisabled();
  });
});
