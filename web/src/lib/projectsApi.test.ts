// Unit tests for `projectsApi.ts` — the `/v1/projects` first-class CRUD
// client. Happy-path requests with a mocked `fetch`, plus error-path coverage
// that surfaces the server's structured `{error: {message}}` shape.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  createProject,
  deleteProject,
  deleteProjectEntry,
  deleteProjectHostBinding,
  deleteProjectRepository,
  getProject,
  getProjectCollaboration,
  listProjectEntries,
  listProjects,
  putProjectEntry,
  putProjectHostBinding,
  putProjectRepository,
  renameProject,
  setProjectCollaborationEnabled,
  updateProjectConfig,
  verifyProjectHostBinding,
} from "./projectsApi";

function mockResponse(body: unknown, init?: { ok?: boolean; status?: number }): Response {
  return {
    ok: init?.ok ?? true,
    status: init?.status ?? 200,
    statusText: "OK",
    json: async () => body,
  } as unknown as Response;
}

const fetchMock = vi.fn();

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("listProjects", () => {
  it("GETs /v1/projects and returns the data array", async () => {
    fetchMock.mockResolvedValueOnce(
      mockResponse({ object: "list", data: [{ id: "p_1", name: "A" }] }),
    );
    const result = await listProjects();
    expect(fetchMock.mock.calls[0][0]).toBe("/v1/projects");
    expect(result).toEqual([{ id: "p_1", name: "A" }]);
  });
});

describe("createProject", () => {
  it("POSTs the name and returns the project", async () => {
    fetchMock.mockResolvedValueOnce(mockResponse({ id: "p_1", name: "New" }));
    const result = await createProject("New");
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/v1/projects");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual({ name: "New" });
    expect(result.id).toBe("p_1");
  });

  it("surfaces the server error message on a duplicate name (409)", async () => {
    fetchMock.mockResolvedValueOnce(
      mockResponse(
        { error: { message: "A project named 'New' already exists" } },
        {
          ok: false,
          status: 409,
        },
      ),
    );
    await expect(createProject("New")).rejects.toThrow("already exists");
  });

  it("includes config in the body when provided", async () => {
    fetchMock.mockResolvedValueOnce(mockResponse({ id: "p_1", name: "New" }));
    await createProject("New", { host_id: "h1", agent_id: "ag_1" });
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(JSON.parse(init.body as string)).toEqual({
      name: "New",
      config: { host_id: "h1", agent_id: "ag_1" },
    });
  });
});

describe("getProject", () => {
  it("GETs /v1/projects/{id} and returns the project with config", async () => {
    fetchMock.mockResolvedValueOnce(
      mockResponse({ id: "p_1", name: "A", config: { workspace: "/w" } }),
    );
    const result = await getProject("p_1");
    expect(fetchMock.mock.calls[0][0]).toBe("/v1/projects/p_1");
    expect(result.config).toEqual({ workspace: "/w" });
  });
});

describe("updateProjectConfig", () => {
  it("PATCHes only the config field (url-encoded id)", async () => {
    fetchMock.mockResolvedValueOnce(
      mockResponse({ id: "p a", name: "A", config: { agent_id: "ag_1" } }),
    );
    await updateProjectConfig("p a", { agent_id: "ag_1" });
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/v1/projects/p%20a");
    expect(init.method).toBe("PATCH");
    expect(JSON.parse(init.body as string)).toEqual({ config: { agent_id: "ag_1" } });
  });

  it("sends config:{} to clear stored defaults", async () => {
    fetchMock.mockResolvedValueOnce(mockResponse({ id: "p_1", name: "A", config: {} }));
    await updateProjectConfig("p_1", {});
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(JSON.parse(init.body as string)).toEqual({ config: {} });
  });
});

describe("renameProject", () => {
  it("PATCHes /v1/projects/{id} with the new name (url-encoded id)", async () => {
    fetchMock.mockResolvedValueOnce(mockResponse({ id: "p a", name: "Renamed" }));
    await renameProject("p a", "Renamed");
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/v1/projects/p%20a");
    expect(init.method).toBe("PATCH");
    expect(JSON.parse(init.body as string)).toEqual({ name: "Renamed" });
  });
});

describe("deleteProject", () => {
  it("DELETEs /v1/projects/{id}", async () => {
    fetchMock.mockResolvedValueOnce(mockResponse({ deleted: true }));
    await deleteProject("p_1");
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/v1/projects/p_1");
    expect(init.method).toBe("DELETE");
  });

  it("throws on non-2xx", async () => {
    fetchMock.mockResolvedValueOnce(mockResponse({}, { ok: false, status: 404 }));
    await expect(deleteProject("missing")).rejects.toThrow();
  });
});

describe("getProjectCollaboration", () => {
  it("GETs /v1/projects/{id}/collaboration", async () => {
    fetchMock.mockResolvedValueOnce(
      mockResponse({ enabled: false, revision: 3, repositories: [], bindings: [], problems: [] }),
    );
    const result = await getProjectCollaboration("p_1");
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/v1/projects/p_1/collaboration");
    // No explicit method — fetch defaults to GET; a POST here must fail.
    expect(init.method).toBeUndefined();
    expect(result.revision).toBe(3);
  });
});

describe("setProjectCollaborationEnabled", () => {
  it("PATCHes the switch with the expected revision", async () => {
    fetchMock.mockResolvedValueOnce(mockResponse({ enabled: true, revision: 4 }));
    const result = await setProjectCollaborationEnabled("p a", true, 3);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/v1/projects/p%20a/collaboration");
    expect(init.method).toBe("PATCH");
    expect(JSON.parse(init.body as string)).toEqual({ enabled: true, expected_revision: 3 });
    expect(result).toEqual({ enabled: true, revision: 4 });
  });
});

describe("putProjectRepository", () => {
  it("PUTs the repository body (url-encoded segments)", async () => {
    fetchMock.mockResolvedValueOnce(mockResponse({ id: "r_1", name: "web" }));
    await putProjectRepository("p_1", "web", {
      remote_url: "https://example.com/web.git",
      default_branch: "main",
    });
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/v1/projects/p_1/repositories/web");
    expect(init.method).toBe("PUT");
    expect(JSON.parse(init.body as string)).toEqual({
      remote_url: "https://example.com/web.git",
      default_branch: "main",
    });
  });
});

describe("deleteProjectRepository", () => {
  it("DELETEs /v1/projects/{id}/repositories/{name}", async () => {
    fetchMock.mockResolvedValueOnce(mockResponse({ deleted: true }));
    await deleteProjectRepository("p_1", "web");
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/v1/projects/p_1/repositories/web");
    expect(init.method).toBe("DELETE");
  });
});

describe("putProjectHostBinding", () => {
  it("PUTs the binding body (url-encoded segments)", async () => {
    fetchMock.mockResolvedValueOnce(mockResponse({ id: "b_1", name: "primary" }));
    await putProjectHostBinding("p_1", "h 1", "primary", {
      workspace: "/repo",
      repository_name: "web",
      is_primary: true,
      enabled: true,
    });
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/v1/projects/p_1/hosts/h%201/bindings/primary");
    expect(init.method).toBe("PUT");
    expect(JSON.parse(init.body as string)).toEqual({
      workspace: "/repo",
      repository_name: "web",
      is_primary: true,
      enabled: true,
    });
  });

  it("surfaces the server code and message on a bad path (400)", async () => {
    fetchMock.mockResolvedValueOnce(
      mockResponse(
        { error: { code: "invalid_input", message: "host stat failed for path '/x'" } },
        { ok: false, status: 400 },
      ),
    );
    const err = await putProjectHostBinding("p_1", "h1", "primary", {
      workspace: "/x",
      repository_name: "web",
    }).catch((e) => e);
    expect(err.status).toBe(400);
    expect(err.code).toBe("invalid_input");
    expect(err.message).toContain("host stat failed");
  });
});

describe("deleteProjectHostBinding", () => {
  it("DELETEs /v1/projects/{id}/hosts/{host_id}/bindings/{name}", async () => {
    fetchMock.mockResolvedValueOnce(mockResponse({ deleted: true }));
    await deleteProjectHostBinding("p_1", "h1", "primary");
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/v1/projects/p_1/hosts/h1/bindings/primary");
    expect(init.method).toBe("DELETE");
  });
});

describe("verifyProjectHostBinding", () => {
  it("POSTs to .../bindings/{name}/verify", async () => {
    fetchMock.mockResolvedValueOnce(mockResponse({ id: "b_1", name: "primary" }));
    await verifyProjectHostBinding("p_1", "h1", "primary");
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/v1/projects/p_1/hosts/h1/bindings/primary/verify");
    expect(init.method).toBe("POST");
  });
});

describe("listProjectEntries", () => {
  it("GETs /v1/projects/{id}/entries and unwraps the entries array", async () => {
    fetchMock.mockResolvedValueOnce(
      mockResponse({
        entries: [{ host_id: "h1", workspace: "/repo", updated_at: null }],
      }),
    );
    const result = await listProjectEntries("p_1");
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/v1/projects/p_1/entries");
    expect(init.method).toBeUndefined();
    expect(result).toEqual([{ host_id: "h1", workspace: "/repo", updated_at: null }]);
  });

  it("throws on non-2xx", async () => {
    fetchMock.mockResolvedValueOnce(mockResponse({}, { ok: false, status: 404 }));
    await expect(listProjectEntries("missing")).rejects.toThrow();
  });
});

describe("putProjectEntry", () => {
  it("PUTs the workspace (url-encoded segments) and returns the entry", async () => {
    fetchMock.mockResolvedValueOnce(
      mockResponse({ host_id: "h 1", workspace: "/repo", updated_at: 5 }),
    );
    const result = await putProjectEntry("p a", "h 1", "/repo");
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/v1/projects/p%20a/entries/h%201");
    expect(init.method).toBe("PUT");
    expect(JSON.parse(init.body as string)).toEqual({ workspace: "/repo" });
    expect(result.workspace).toBe("/repo");
  });

  it("surfaces the server message on an offline host (409)", async () => {
    fetchMock.mockResolvedValueOnce(
      mockResponse(
        { error: { code: "conflict", message: "host is offline" } },
        { ok: false, status: 409 },
      ),
    );
    const err = await putProjectEntry("p_1", "h1", "/repo").catch((e) => e);
    expect(err.status).toBe(409);
    expect(err.message).toBe("host is offline");
  });
});

describe("deleteProjectEntry", () => {
  it("DELETEs /v1/projects/{id}/entries/{host_id}", async () => {
    fetchMock.mockResolvedValueOnce(mockResponse(null, { status: 204 }));
    await deleteProjectEntry("p_1", "h1");
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/v1/projects/p_1/entries/h1");
    expect(init.method).toBe("DELETE");
  });

  it("surfaces the server message when the entry is absent (404)", async () => {
    fetchMock.mockResolvedValueOnce(
      mockResponse(
        { error: { code: "not_found", message: "Entry not found" } },
        { ok: false, status: 404 },
      ),
    );
    await expect(deleteProjectEntry("p_1", "h1")).rejects.toThrow("Entry not found");
  });
});
