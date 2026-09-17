import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { ProjectCollaborationSection } from "./ProjectCollaborationSection";
import {
  deleteProjectHostBinding,
  deleteProjectRepository,
  getProjectCollaboration,
  putProjectHostBinding,
  putProjectRepository,
  setProjectCollaborationEnabled,
  verifyProjectHostBinding,
  type ProjectCollaboration,
} from "@/lib/projectsApi";
import { ApiError } from "@/lib/sessionsApi";

vi.mock("@/lib/projectsApi", () => ({
  deleteProjectHostBinding: vi.fn(),
  deleteProjectRepository: vi.fn(),
  getProjectCollaboration: vi.fn(),
  putProjectHostBinding: vi.fn(),
  putProjectRepository: vi.fn(),
  setProjectCollaborationEnabled: vi.fn(),
  verifyProjectHostBinding: vi.fn(),
}));
vi.mock("@/hooks/useHosts", () => ({
  useHosts: () => ({
    data: [
      { host_id: "h1", name: "Laptop", owner: "me", status: "online" },
      {
        host_id: "sandbox-1",
        name: "Sandbox",
        owner: "me",
        status: "online",
        sandbox_provider: "modal",
      },
    ],
  }),
}));

const getMock = vi.mocked(getProjectCollaboration);
const setEnabledMock = vi.mocked(setProjectCollaborationEnabled);
const putRepoMock = vi.mocked(putProjectRepository);
const putBindingMock = vi.mocked(putProjectHostBinding);

function collaboration(overrides: Partial<ProjectCollaboration> = {}): ProjectCollaboration {
  return {
    enabled: false,
    revision: 3,
    repositories: [],
    bindings: [],
    problems: [],
    ...overrides,
  };
}

function repo(overrides: Record<string, unknown> = {}) {
  return {
    id: "r_1",
    project_id: "p_1",
    name: "web",
    remote_url: "https://example.com/web.git",
    default_branch: "main",
    context_manifest_path: ".agents/project/manifest.json",
    revision: 1,
    created_at: 1,
    updated_at: null,
    ...overrides,
  };
}

function binding(overrides: Record<string, unknown> = {}) {
  return {
    id: "b_1",
    project_id: "p_1",
    host_id: "h1",
    name: "primary",
    is_primary: true,
    repository_id: "r_1",
    workspace: "/repo",
    enabled: true,
    revision: 1,
    path_verified_at: 1,
    created_at: 1,
    updated_at: null,
    ...overrides,
  };
}

function renderSection() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <ProjectCollaborationSection projectId="p_1" />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  getMock.mockReset();
  setEnabledMock.mockReset();
  putRepoMock.mockReset();
  putBindingMock.mockReset();
  vi.mocked(deleteProjectRepository).mockReset();
  vi.mocked(deleteProjectHostBinding).mockReset();
  vi.mocked(verifyProjectHostBinding).mockReset();
});

afterEach(cleanup);

describe("ProjectCollaborationSection", () => {
  it("sends the loaded revision when toggling collaboration on", async () => {
    getMock.mockResolvedValue(collaboration());
    setEnabledMock.mockResolvedValue({ enabled: true, revision: 4 });
    renderSection();
    await waitFor(() =>
      expect(screen.getByTestId("project-collaboration-enabled")).toBeInTheDocument(),
    );

    fireEvent.click(screen.getByTestId("project-collaboration-enabled"));

    await waitFor(() => expect(setEnabledMock).toHaveBeenCalledWith("p_1", true, 3));
  });

  it("shows the conflict notice, refetches, and reflects the server state on a 409", async () => {
    // First read (revision 3, off); the refetch after the conflict reads the
    // writer's newer state (revision 4, on) — the switch must follow the server.
    getMock
      .mockResolvedValueOnce(collaboration({ enabled: false, revision: 3 }))
      .mockResolvedValue(collaboration({ enabled: true, revision: 4 }));
    setEnabledMock.mockRejectedValueOnce(new ApiError("revision mismatch", 409, "conflict"));
    renderSection();
    await waitFor(() =>
      expect(screen.getByTestId("project-collaboration-enabled")).toBeInTheDocument(),
    );

    fireEvent.click(screen.getByTestId("project-collaboration-enabled"));

    await waitFor(() =>
      expect(screen.getByTestId("project-collaboration-error")).toHaveTextContent(
        "Collaboration settings changed elsewhere; the latest settings are shown. Try again.",
      ),
    );
    await waitFor(() => expect(getMock).toHaveBeenCalledTimes(2));
    await waitFor(() =>
      expect(screen.getByTestId("project-collaboration-enabled")).toHaveAttribute(
        "data-state",
        "checked",
      ),
    );
  });

  it("adds a repository, omitting a blank manifest path", async () => {
    getMock.mockResolvedValue(collaboration());
    putRepoMock.mockResolvedValue(repo());
    renderSection();
    await waitFor(() =>
      expect(screen.getByTestId("project-collaboration-repo-add")).toBeInTheDocument(),
    );

    fireEvent.change(screen.getByTestId("project-collaboration-repo-name"), {
      target: { value: "web" },
    });
    fireEvent.change(screen.getByTestId("project-collaboration-repo-url"), {
      target: { value: "https://example.com/web.git" },
    });
    fireEvent.submit(screen.getByTestId("project-collaboration-repo-add").closest("form")!);

    await waitFor(() => expect(putRepoMock).toHaveBeenCalled());
    expect(putRepoMock).toHaveBeenCalledWith("p_1", "web", {
      remote_url: "https://example.com/web.git",
      default_branch: "main",
    });
    expect(putRepoMock.mock.calls[0]![2]).not.toHaveProperty("context_manifest_path");
  });

  it("adds a binding with host, name, and body", async () => {
    const added = {
      id: "b_1",
      project_id: "p_1",
      host_id: "h1",
      name: "primary",
      is_primary: true,
      repository_id: "r_1",
      workspace: "/repo",
      enabled: true,
      revision: 1,
      path_verified_at: 1,
      created_at: 1,
      updated_at: null,
    };
    // Initial read has no bindings; the refetch after the add returns the new
    // binding, whose row must render.
    getMock
      .mockResolvedValueOnce(collaboration({ repositories: [repo()] }))
      .mockResolvedValue(collaboration({ repositories: [repo()], bindings: [added] }));
    putBindingMock.mockResolvedValue(added);
    renderSection();
    await waitFor(() =>
      expect(screen.getByTestId("project-collaboration-binding-add")).toBeInTheDocument(),
    );

    // Sandbox hosts are never binding targets.
    const hostOptions = Array.from(
      (screen.getByTestId("project-collaboration-binding-host") as HTMLSelectElement).options,
    ).map((o) => o.value);
    expect(hostOptions).toEqual(["h1"]);

    fireEvent.change(screen.getByTestId("project-collaboration-binding-workspace"), {
      target: { value: "/repo" },
    });
    fireEvent.submit(screen.getByTestId("project-collaboration-binding-add").closest("form")!);

    await waitFor(() => expect(putBindingMock).toHaveBeenCalled());
    expect(putBindingMock).toHaveBeenCalledWith("p_1", "h1", "primary", {
      workspace: "/repo",
      repository_name: "web",
      is_primary: true,
    });
    // The refetched binding renders under its host's group.
    await waitFor(() =>
      expect(
        screen.getByTestId("project-collaboration-binding-verify-h1-primary"),
      ).toBeInTheDocument(),
    );
    // Host select option + the binding's host group header.
    expect(screen.getAllByText("Laptop")).toHaveLength(2);
    expect(screen.getByText("web · /repo")).toBeInTheDocument();
  });

  it("warns with exit code and output when the post-bind command fails", async () => {
    const added = binding({
      post_bind: { status: "failed", exit_code: 3, output: "boom", error: null },
    });
    // The initial read has no bindings; the refetch after the add returns the
    // new binding, so a failing hook can never hide the stored row.
    getMock
      .mockResolvedValueOnce(collaboration({ repositories: [repo()] }))
      .mockResolvedValue(collaboration({ repositories: [repo()], bindings: [added] }));
    putBindingMock.mockResolvedValue(added);
    renderSection();
    await waitFor(() =>
      expect(screen.getByTestId("project-collaboration-binding-add")).toBeInTheDocument(),
    );

    fireEvent.change(screen.getByTestId("project-collaboration-binding-workspace"), {
      target: { value: "/repo" },
    });
    fireEvent.submit(screen.getByTestId("project-collaboration-binding-add").closest("form")!);

    await waitFor(() =>
      expect(screen.getByTestId("project-collaboration-hook-warning")).toHaveTextContent(
        /Binding saved; post-bind command failed/,
      ),
    );
    const warning = screen.getByTestId("project-collaboration-hook-warning");
    expect(warning).toHaveTextContent("exit code 3");
    expect(warning).toHaveTextContent("boom");
    await waitFor(() =>
      expect(
        screen.getByTestId("project-collaboration-binding-verify-h1-primary"),
      ).toBeInTheDocument(),
    );
  });

  it("shows no warning when the host has no post-bind command configured", async () => {
    const added = binding({
      post_bind: { status: "not_configured", exit_code: null, output: null, error: null },
    });
    getMock
      .mockResolvedValueOnce(collaboration({ repositories: [repo()] }))
      .mockResolvedValue(collaboration({ repositories: [repo()], bindings: [added] }));
    putBindingMock.mockResolvedValue(added);
    renderSection();
    await waitFor(() =>
      expect(screen.getByTestId("project-collaboration-binding-add")).toBeInTheDocument(),
    );

    fireEvent.change(screen.getByTestId("project-collaboration-binding-workspace"), {
      target: { value: "/repo" },
    });
    fireEvent.submit(screen.getByTestId("project-collaboration-binding-add").closest("form")!);

    await waitFor(() =>
      expect(
        screen.getByTestId("project-collaboration-binding-verify-h1-primary"),
      ).toBeInTheDocument(),
    );
    expect(screen.queryByTestId("project-collaboration-hook-warning")).not.toBeInTheDocument();
  });

  it("shows the post-bind error when verify reports the host unreachable", async () => {
    getMock.mockResolvedValue(collaboration({ repositories: [repo()], bindings: [binding()] }));
    vi.mocked(verifyProjectHostBinding).mockResolvedValue(
      binding({
        post_bind: {
          status: "unreachable",
          exit_code: null,
          output: null,
          error: "no live connection to the host",
        },
      }),
    );
    renderSection();
    await waitFor(() =>
      expect(
        screen.getByTestId("project-collaboration-binding-verify-h1-primary"),
      ).toBeInTheDocument(),
    );

    fireEvent.click(screen.getByTestId("project-collaboration-binding-verify-h1-primary"));

    await waitFor(() =>
      expect(screen.getByTestId("project-collaboration-hook-warning")).toHaveTextContent(
        "no live connection to the host",
      ),
    );
  });

  it("disables the repository form while the add is pending", async () => {
    getMock.mockResolvedValue(collaboration());
    let resolveAdd: ((value: Awaited<ReturnType<typeof putProjectRepository>>) => void) | undefined;
    putRepoMock.mockImplementationOnce(
      () =>
        new Promise<Awaited<ReturnType<typeof putProjectRepository>>>((resolve) => {
          resolveAdd = resolve;
        }),
    );
    renderSection();
    await waitFor(() =>
      expect(screen.getByTestId("project-collaboration-repo-add")).toBeInTheDocument(),
    );

    fireEvent.change(screen.getByTestId("project-collaboration-repo-name"), {
      target: { value: "web" },
    });
    fireEvent.change(screen.getByTestId("project-collaboration-repo-url"), {
      target: { value: "https://example.com/web.git" },
    });
    fireEvent.submit(screen.getByTestId("project-collaboration-repo-add").closest("form")!);

    // The draft stays on screen but locked, so a success can't wipe input
    // typed during the request.
    await waitFor(() =>
      expect(screen.getByTestId("project-collaboration-repo-name")).toBeDisabled(),
    );
    expect(screen.getByTestId("project-collaboration-repo-url")).toBeDisabled();
    expect(screen.getByTestId("project-collaboration-repo-add")).toBeDisabled();
    resolveAdd!(repo());
    await waitFor(() =>
      expect(screen.getByTestId("project-collaboration-repo-name")).toBeEnabled(),
    );
  });

  it("disables the binding form while the add is pending", async () => {
    getMock.mockResolvedValue(collaboration({ repositories: [repo()] }));
    let resolveAdd:
      ((value: Awaited<ReturnType<typeof putProjectHostBinding>>) => void) | undefined;
    putBindingMock.mockImplementationOnce(
      () =>
        new Promise<Awaited<ReturnType<typeof putProjectHostBinding>>>((resolve) => {
          resolveAdd = resolve;
        }),
    );
    renderSection();
    await waitFor(() =>
      expect(screen.getByTestId("project-collaboration-binding-add")).toBeInTheDocument(),
    );

    fireEvent.change(screen.getByTestId("project-collaboration-binding-workspace"), {
      target: { value: "/repo" },
    });
    fireEvent.submit(screen.getByTestId("project-collaboration-binding-add").closest("form")!);

    await waitFor(() =>
      expect(screen.getByTestId("project-collaboration-binding-workspace")).toBeDisabled(),
    );
    expect(screen.getByTestId("project-collaboration-binding-add")).toBeDisabled();
    resolveAdd!({
      id: "b_1",
      project_id: "p_1",
      host_id: "h1",
      name: "primary",
      is_primary: true,
      repository_id: "r_1",
      workspace: "/repo",
      enabled: true,
      revision: 1,
      path_verified_at: 1,
      created_at: 1,
      updated_at: null,
    });
    await waitFor(() =>
      expect(screen.getByTestId("project-collaboration-binding-workspace")).toBeEnabled(),
    );
  });

  it("sends the remaining repository after the selected one is removed", async () => {
    const api = repo({
      id: "r_api",
      name: "api",
      remote_url: "https://example.com/api.git",
    });
    const web = repo();
    // Select `api`, then remove it while `web` remains — the select falls back
    // to `web` and the next add must send `web`, not the stale `api`.
    getMock
      .mockResolvedValueOnce(collaboration({ repositories: [api, web] }))
      .mockResolvedValue(collaboration({ repositories: [web] }));
    vi.mocked(deleteProjectRepository).mockResolvedValueOnce(undefined);
    putBindingMock.mockResolvedValue({
      id: "b_1",
      project_id: "p_1",
      host_id: "h1",
      name: "primary",
      is_primary: true,
      repository_id: "r_1",
      workspace: "/repo",
      enabled: true,
      revision: 1,
      path_verified_at: 1,
      created_at: 1,
      updated_at: null,
    });
    renderSection();
    await waitFor(() =>
      expect(screen.getByTestId("project-collaboration-binding-repo")).toBeInTheDocument(),
    );

    fireEvent.change(screen.getByTestId("project-collaboration-binding-repo"), {
      target: { value: "api" },
    });
    fireEvent.click(screen.getByTestId("project-collaboration-repo-remove-api"));

    await waitFor(() =>
      expect(
        (screen.getByTestId("project-collaboration-binding-repo") as HTMLSelectElement).value,
      ).toBe("web"),
    );
    fireEvent.change(screen.getByTestId("project-collaboration-binding-workspace"), {
      target: { value: "/repo" },
    });
    fireEvent.submit(screen.getByTestId("project-collaboration-binding-add").closest("form")!);

    await waitFor(() => expect(putBindingMock).toHaveBeenCalled());
    expect(putBindingMock).toHaveBeenCalledWith("p_1", "h1", "primary", {
      workspace: "/repo",
      repository_name: "web",
      is_primary: true,
    });
  });

  it("shows the server message verbatim and adds no row when the host rejects the path", async () => {
    const message = "host stat failed for path '/does/not/exist': No such file or directory";
    getMock.mockResolvedValue(collaboration({ repositories: [repo()] }));
    putBindingMock.mockRejectedValueOnce(new ApiError(message, 400, "invalid_input"));
    renderSection();
    await waitFor(() =>
      expect(screen.getByTestId("project-collaboration-binding-add")).toBeInTheDocument(),
    );

    fireEvent.change(screen.getByTestId("project-collaboration-binding-workspace"), {
      target: { value: "/does/not/exist" },
    });
    fireEvent.submit(screen.getByTestId("project-collaboration-binding-add").closest("form")!);

    await waitFor(() =>
      expect(screen.getByTestId("project-collaboration-error")).toHaveTextContent(message),
    );
    expect(
      screen.queryByTestId("project-collaboration-binding-verify-h1-primary"),
    ).not.toBeInTheDocument();
  });

  it("renders one line per problem, naming the host", async () => {
    getMock.mockResolvedValue(
      collaboration({
        bindings: [
          {
            id: "b_1",
            project_id: "p_1",
            host_id: "h1",
            name: "extra",
            is_primary: false,
            repository_id: "r_gone",
            workspace: "/repo",
            enabled: true,
            revision: 1,
            path_verified_at: null,
            created_at: 1,
            updated_at: null,
          },
        ],
        problems: [
          { code: "missing_primary", host_id: "h1" },
          {
            code: "dangling_repository",
            binding_id: "b_1",
            host_id: "h1",
            repository_id: "r_gone",
          },
        ],
      }),
    );
    renderSection();

    await waitFor(() =>
      expect(screen.getAllByTestId("project-collaboration-problem")).toHaveLength(2),
    );
    const lines = screen.getAllByTestId("project-collaboration-problem");
    expect(lines[0]).toHaveTextContent(/Laptop/);
    expect(lines[0]).toHaveTextContent(/no primary binding/);
    expect(lines[1]).toHaveTextContent(/Laptop/);
    expect(lines[1]).toHaveTextContent(/no longer registered/);
  });

  it("shows a load error with no controls when the fetch fails", async () => {
    getMock.mockRejectedValueOnce(new Error("500 Server Error"));
    renderSection();

    await waitFor(() =>
      expect(screen.getByTestId("project-collaboration-error")).toBeInTheDocument(),
    );
    expect(screen.queryByTestId("project-collaboration-enabled")).not.toBeInTheDocument();
    expect(screen.queryByTestId("project-collaboration-repo-add")).not.toBeInTheDocument();
  });
});
