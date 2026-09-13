// Unit tests for `identity.ts` — `resolveIdentity()` discovery and
// `authenticatedFetch()` header injection.
//
// `identity.ts` keeps its cached user id at module scope (the entire
// app shares one identity), so each test calls `vi.resetModules()` and
// re-imports to start from a clean slate. Otherwise tests would leak
// state into each other through the cached `currentUserId`.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

function mockJsonResponse(body: unknown, init?: { ok?: boolean; status?: number }): Response {
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
  vi.resetModules();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("resolveIdentity", () => {
  it("discards a body decoded after switching Servers before a new identity lookup", async () => {
    let finishBody!: (body: unknown) => void;
    const json = vi.fn(
      () =>
        new Promise<unknown>((resolve) => {
          finishBody = resolve;
        }),
    );
    const serverA = vi.fn().mockResolvedValue({ ok: true, status: 200, json });
    const serverB = vi.fn().mockResolvedValue(mockJsonResponse({ user_id: "bob" }));
    const { setOmnigentHostConfig } = await import("./host");
    setOmnigentHostConfig({ serverIdentity: "server-a", fetcher: serverA });
    const { resolveIdentity } = await import("./identity");
    const pending = resolveIdentity();
    await vi.waitFor(() => expect(json).toHaveBeenCalledOnce());
    setOmnigentHostConfig({ serverIdentity: "server-b", fetcher: serverB });
    finishBody({ user_id: "alice" });
    expect(await pending).toBeNull();
    expect(serverB).not.toHaveBeenCalled();
  });

  it("calls GET /v1/me and caches the user id", async () => {
    fetchMock.mockResolvedValueOnce(mockJsonResponse({ user_id: "alice@example.com" }));
    const { resolveIdentity, getCurrentUserId } = await import("./identity");

    const userId = await resolveIdentity();

    expect(userId).toBe("alice@example.com");
    expect(getCurrentUserId()).toBe("alice@example.com");
    expect(fetchMock).toHaveBeenCalledOnce();
    expect(fetchMock.mock.calls[0][0]).toBe("/v1/me");
  });

  it("returns the cached value on subsequent calls without re-fetching", async () => {
    fetchMock.mockResolvedValueOnce(mockJsonResponse({ user_id: "bob" }));
    const { resolveIdentity } = await import("./identity");

    const first = await resolveIdentity();
    const second = await resolveIdentity();

    expect(first).toBe("bob");
    expect(second).toBe("bob");
    // Critical: a second call MUST NOT hit the network. If this fires
    // twice we're paying a round-trip on every component mount.
    expect(fetchMock).toHaveBeenCalledOnce();
  });

  it("dedupes concurrent calls into a single in-flight request", async () => {
    // Two callers race resolveIdentity() before the first has settled.
    // Both should resolve to the same user id from one fetch — without
    // dedupe, the cache would get populated twice and `fetch` would
    // fire twice.
    let resolveBody: ((r: Response) => void) | null = null;
    fetchMock.mockReturnValueOnce(
      new Promise<Response>((r) => {
        resolveBody = r;
      }),
    );
    const { resolveIdentity } = await import("./identity");

    const a = resolveIdentity();
    const b = resolveIdentity();
    expect(fetchMock).toHaveBeenCalledOnce();

    resolveBody!(mockJsonResponse({ user_id: "carol" }));
    expect(await a).toBe("carol");
    expect(await b).toBe("carol");
    expect(fetchMock).toHaveBeenCalledOnce();
  });

  it("refreshes the cached identity after the embedded Server connection changes", async () => {
    const serverA = vi.fn().mockResolvedValue(mockJsonResponse({ user_id: "alice" }));
    const serverB = vi.fn().mockResolvedValue(mockJsonResponse({ user_id: "bob" }));
    const { setOmnigentHostConfig } = await import("./host");
    setOmnigentHostConfig({ serverIdentity: "server-a", fetcher: serverA });
    const { resolveIdentity, getCurrentUserId } = await import("./identity");

    expect(await resolveIdentity()).toBe("alice");
    setOmnigentHostConfig({ serverIdentity: "server-b", fetcher: serverB });

    expect(await resolveIdentity()).toBe("bob");
    expect(getCurrentUserId()).toBe("bob");
    expect(serverA).toHaveBeenCalledOnce();
    expect(serverB).toHaveBeenCalledOnce();
  });

  it("discards a delayed identity response from the previous Server", async () => {
    let resolveServerA!: (response: Response) => void;
    const serverA = vi.fn().mockReturnValue(
      new Promise<Response>((resolve) => {
        resolveServerA = resolve;
      }),
    );
    const serverB = vi.fn().mockResolvedValue(mockJsonResponse({ user_id: "bob" }));
    const { setOmnigentHostConfig } = await import("./host");
    setOmnigentHostConfig({ serverIdentity: "server-a", fetcher: serverA });
    const { resolveIdentity, getCurrentUserId } = await import("./identity");

    const staleResolution = resolveIdentity();
    setOmnigentHostConfig({ serverIdentity: "server-b", fetcher: serverB });
    const currentResolution = resolveIdentity();
    resolveServerA(mockJsonResponse({ user_id: "alice" }));

    expect(await staleResolution).toBeNull();
    expect(await currentResolution).toBe("bob");
    expect(getCurrentUserId()).toBe("bob");
    expect(serverA).toHaveBeenCalledOnce();
    expect(serverB).toHaveBeenCalledOnce();
  });

  it("discards a lookup from before a round trip back to the same Server", async () => {
    let resolveServerA1!: (response: Response) => void;
    const serverA1 = vi.fn().mockReturnValue(
      new Promise<Response>((resolve) => {
        resolveServerA1 = resolve;
      }),
    );
    const serverB = vi.fn().mockResolvedValue(mockJsonResponse({ user_id: "bob" }));
    const serverA2 = vi
      .fn()
      .mockResolvedValue(mockJsonResponse({ user_id: "alice", is_admin: true }));
    const { setOmnigentHostConfig } = await import("./host");
    setOmnigentHostConfig({ serverIdentity: "server-a", fetcher: serverA1 });
    const { resolveIdentity, getCurrentUserId, getCurrentIsAdmin } = await import("./identity");

    const staleResolution = resolveIdentity();
    setOmnigentHostConfig({ serverIdentity: "server-b", fetcher: serverB });
    expect(await resolveIdentity()).toBe("bob");
    setOmnigentHostConfig({ serverIdentity: "server-a", fetcher: serverA2 });
    expect(await resolveIdentity()).toBe("alice");
    resolveServerA1(mockJsonResponse({ user_id: "mallory", is_admin: false }));

    expect(await staleResolution).toBeNull();
    expect(getCurrentUserId()).toBe("alice");
    expect(getCurrentIsAdmin()).toBe(true);
  });

  it("starts a new lookup when returning to a Server whose lookup was discarded", async () => {
    let resolveServerA1!: (response: Response) => void;
    const serverA1 = vi.fn().mockReturnValue(
      new Promise<Response>((resolve) => {
        resolveServerA1 = resolve;
      }),
    );
    const serverB = vi.fn().mockResolvedValue(mockJsonResponse({ user_id: "bob" }));
    const serverA2 = vi.fn().mockResolvedValue(mockJsonResponse({ user_id: "alice" }));
    const { setOmnigentHostConfig } = await import("./host");
    setOmnigentHostConfig({ serverIdentity: "server-a", fetcher: serverA1 });
    const { resolveIdentity } = await import("./identity");

    const staleResolution = resolveIdentity();
    setOmnigentHostConfig({ serverIdentity: "server-b", fetcher: serverB });
    resolveServerA1(mockJsonResponse({ user_id: "alice" }));
    expect(await staleResolution).toBeNull();
    setOmnigentHostConfig({ serverIdentity: "server-a", fetcher: serverA2 });
    expect(await resolveIdentity()).toBe("alice");
    expect(serverA2).toHaveBeenCalledOnce();
  });

  it("drops the cached user and header after switching Servers without resolving", async () => {
    const serverA = vi
      .fn()
      .mockResolvedValue(mockJsonResponse({ user_id: "alice", is_admin: true }));
    const serverB = vi.fn().mockResolvedValue(mockJsonResponse({}));
    const { setOmnigentHostConfig } = await import("./host");
    setOmnigentHostConfig({ serverIdentity: "server-a", fetcher: serverA });
    const { resolveIdentity, getCurrentUserId, getCurrentIsAdmin, authenticatedFetch } =
      await import("./identity");

    expect(await resolveIdentity()).toBe("alice");
    setOmnigentHostConfig({ serverIdentity: "server-b", fetcher: serverB });

    expect(getCurrentUserId()).toBeNull();
    expect(getCurrentIsAdmin()).toBe(false);
    await authenticatedFetch("/v1/sessions");
    const init = serverB.mock.calls[0][1] as RequestInit;
    const headers = new Headers(init.headers);
    expect(headers.has("X-Forwarded-Email")).toBe(false);
  });

  it("keeps the identity when the same Server config is installed again", async () => {
    const serverA = vi
      .fn()
      .mockResolvedValue(mockJsonResponse({ user_id: "alice", is_admin: true }));
    const anotherFetcher = vi.fn().mockResolvedValue(mockJsonResponse({ user_id: "bob" }));
    const { setOmnigentHostConfig } = await import("./host");
    setOmnigentHostConfig({ serverIdentity: "server-a", fetcher: serverA });
    const { resolveIdentity, getCurrentUserId, getCurrentIsAdmin } = await import("./identity");

    expect(await resolveIdentity()).toBe("alice");
    setOmnigentHostConfig({ serverIdentity: "server-a", fetcher: anotherFetcher });

    expect(getCurrentUserId()).toBe("alice");
    expect(getCurrentIsAdmin()).toBe(true);
    expect(await resolveIdentity()).toBe("alice");
    expect(serverA).toHaveBeenCalledOnce();
    expect(anotherFetcher).not.toHaveBeenCalled();
  });

  it("starts a new identity lookup for each config install without a Server identity", async () => {
    const fetcherA = vi.fn().mockResolvedValue(mockJsonResponse({ user_id: "alice" }));
    const fetcherB = vi.fn().mockResolvedValue(mockJsonResponse({ user_id: "bob" }));
    const { setOmnigentHostConfig } = await import("./host");
    setOmnigentHostConfig({ fetcher: fetcherA });
    const { resolveIdentity, getCurrentUserId } = await import("./identity");

    expect(await resolveIdentity()).toBe("alice");
    setOmnigentHostConfig({ fetcher: fetcherB });

    expect(getCurrentUserId()).toBeNull();
    expect(await resolveIdentity()).toBe("bob");
    expect(fetcherA).toHaveBeenCalledOnce();
    expect(fetcherB).toHaveBeenCalledOnce();
  });

  it("returns null when the server responds with user_id: null", async () => {
    // Server signals "no auth provider configured" with user_id: null.
    // Resolution should still complete (not throw) so the app can
    // continue without sending the header.
    fetchMock.mockResolvedValueOnce(mockJsonResponse({ user_id: null }));
    const { resolveIdentity, getCurrentUserId } = await import("./identity");

    const userId = await resolveIdentity();

    expect(userId).toBeNull();
    expect(getCurrentUserId()).toBeNull();
  });

  it("swallows network errors and resolves to null", async () => {
    // If the server is unreachable we can't block app startup. The
    // promise must resolve (not reject) and `getCurrentUserId` returns
    // null. authenticatedFetch then becomes a passthrough.
    fetchMock.mockRejectedValueOnce(new Error("network"));
    const { resolveIdentity, getCurrentUserId } = await import("./identity");

    const userId = await resolveIdentity();

    expect(userId).toBeNull();
    expect(getCurrentUserId()).toBeNull();
  });

  it("treats non-2xx as null without throwing", async () => {
    fetchMock.mockResolvedValueOnce(mockJsonResponse({}, { ok: false, status: 500 }));
    const { resolveIdentity } = await import("./identity");

    await expect(resolveIdentity()).resolves.toBeNull();
  });
});

describe("getCurrentUserId", () => {
  it("returns null before resolveIdentity has been called", async () => {
    const { getCurrentUserId } = await import("./identity");
    expect(getCurrentUserId()).toBeNull();
  });
});

describe("authenticatedFetch", () => {
  it("injects X-Forwarded-Email header once the identity is resolved", async () => {
    fetchMock.mockResolvedValueOnce(mockJsonResponse({ user_id: "alice" }));
    const { resolveIdentity, authenticatedFetch } = await import("./identity");
    await resolveIdentity();

    fetchMock.mockResolvedValueOnce(mockJsonResponse({}));
    await authenticatedFetch("/v1/sessions");

    const init = fetchMock.mock.calls[1][1] as RequestInit;
    const headers = new Headers(init.headers);
    expect(headers.get("X-Forwarded-Email")).toBe("alice");
  });

  it("does NOT inject the header when identity is unresolved", async () => {
    // Before `resolveIdentity()` runs, the cache is null. We must not
    // send `X-Forwarded-Email: null` (which the server would reject in
    // multi-user mode) — pass the request through untouched.
    const { authenticatedFetch } = await import("./identity");

    fetchMock.mockResolvedValueOnce(mockJsonResponse({}));
    await authenticatedFetch("/v1/sessions");

    const init = fetchMock.mock.calls[0][1] as RequestInit | undefined;
    if (init?.headers) {
      const headers = new Headers(init.headers);
      expect(headers.has("X-Forwarded-Email")).toBe(false);
    }
  });

  it("preserves caller-supplied headers when injecting", async () => {
    // The caller may already pass Content-Type, Accept, etc. Those
    // must survive the merge with the auth header.
    fetchMock.mockResolvedValueOnce(mockJsonResponse({ user_id: "alice" }));
    const { resolveIdentity, authenticatedFetch } = await import("./identity");
    await resolveIdentity();

    fetchMock.mockResolvedValueOnce(mockJsonResponse({}));
    await authenticatedFetch("/v1/sessions", {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
      body: "{}",
    });

    const init = fetchMock.mock.calls[1][1] as RequestInit;
    const headers = new Headers(init.headers);
    expect(headers.get("X-Forwarded-Email")).toBe("alice");
    expect(headers.get("Content-Type")).toBe("application/json");
    expect(headers.get("Accept")).toBe("text/event-stream");
    expect(init.method).toBe("POST");
    expect(init.body).toBe("{}");
  });

  it("does not overwrite an explicit X-Forwarded-Email the caller set", async () => {
    // Edge case: a caller (test, debug tool, future explicit-impersonate
    // flow) may set X-Forwarded-Email itself. Don't clobber it — the
    // identity layer is a default, not an override.
    fetchMock.mockResolvedValueOnce(mockJsonResponse({ user_id: "alice" }));
    const { resolveIdentity, authenticatedFetch } = await import("./identity");
    await resolveIdentity();

    fetchMock.mockResolvedValueOnce(mockJsonResponse({}));
    await authenticatedFetch("/v1/sessions", {
      headers: { "X-Forwarded-Email": "explicit-override" },
    });

    const init = fetchMock.mock.calls[1][1] as RequestInit;
    const headers = new Headers(init.headers);
    expect(headers.get("X-Forwarded-Email")).toBe("explicit-override");
  });

  it("forwards method, body, and signal", async () => {
    fetchMock.mockResolvedValueOnce(mockJsonResponse({ user_id: "alice" }));
    const { resolveIdentity, authenticatedFetch } = await import("./identity");
    await resolveIdentity();

    const controller = new AbortController();
    fetchMock.mockResolvedValueOnce(mockJsonResponse({}));
    await authenticatedFetch("/v1/sessions/x", {
      method: "DELETE",
      signal: controller.signal,
    });

    const init = fetchMock.mock.calls[1][1] as RequestInit;
    expect(init.method).toBe("DELETE");
    expect(init.signal).toBe(controller.signal);
  });

  it("does not retry a wrong-replica request on a Server selected after dispatch", async () => {
    let releaseRunners!: (res: Response) => void;
    const runnersPending = new Promise<Response>((resolve) => {
      releaseRunners = resolve;
    });
    const serverA = vi.fn((path: string) => {
      if (path === "/v1/me") return Promise.resolve(mockJsonResponse({ user_id: "alice" }));
      if (path === "/v1/hosts/host-a/runners") return runnersPending;
      return Promise.resolve(mockJsonResponse({}));
    });
    const serverB = vi.fn((path: string, init?: RequestInit) => {
      void init;
      return Promise.resolve(mockJsonResponse(path === "/v1/me" ? { user_id: "bob" } : {}));
    });
    const { setOmnigentHostConfig } = await import("./host");
    setOmnigentHostConfig({ serverIdentity: "server-a", fetcher: serverA });
    const { resolveIdentity, authenticatedFetch } = await import("./identity");

    expect(await resolveIdentity()).toBe("alice");
    const pending = authenticatedFetch("/v1/hosts/host-a/runners", { method: "POST" });
    await vi.waitFor(() =>
      expect(serverA).toHaveBeenCalledWith("/v1/hosts/host-a/runners", expect.anything()),
    );
    setOmnigentHostConfig({ serverIdentity: "server-b", fetcher: serverB });
    expect(await resolveIdentity()).toBe("bob");
    releaseRunners({
      ok: false,
      status: 400,
      statusText: "Bad Request",
      json: async () => ({ error: { code: "wrong_replica" } }),
      clone: function () {
        return this;
      },
    } as unknown as Response);

    const res = await pending;
    expect(res.status).toBe(400);
    expect(serverB.mock.calls.map((call) => call[0])).toEqual(["/v1/me"]);
    for (const call of serverB.mock.calls) {
      const headers = new Headers(call[1]?.headers);
      expect(headers.get("X-Forwarded-Email")).not.toBe("alice");
    }
  });

  it("keeps the header decision from the start of a request that waits for its session host", async () => {
    const { setOmnigentHostConfig } = await import("./host");
    const { setSessionHostResolver, resolveIdentity, authenticatedFetch } =
      await import("./identity");

    let releaseFirst!: () => void;
    const firstGate = new Promise<void>((resolve) => {
      releaseFirst = resolve;
    });
    let releaseSecond!: () => void;
    const secondGate = new Promise<void>((resolve) => {
      releaseSecond = resolve;
    });
    const resolver = vi.fn((sessionId: string) => {
      if (sessionId === "sess-wait-unresolved") return firstGate;
      return secondGate;
    });
    setSessionHostResolver(resolver);

    const fetcher = vi.fn((path: string, init?: RequestInit) => {
      void init;
      if (path === "/v1/me") return Promise.resolve(mockJsonResponse({ user_id: "alice" }));
      return Promise.resolve(mockJsonResponse({}));
    });
    setOmnigentHostConfig({ serverIdentity: "server-a", fetcher });

    const pendingUnresolved = authenticatedFetch(
      "/v1/sessions/sess-wait-unresolved/resources/terminals",
    );
    await vi.waitFor(() => expect(resolver).toHaveBeenCalledWith("sess-wait-unresolved"));
    expect(await resolveIdentity()).toBe("alice");
    releaseFirst();
    await pendingUnresolved;

    const unresolvedCall = fetcher.mock.calls.find(([path]) => path !== "/v1/me");
    expect(unresolvedCall).toBeDefined();
    expect(new Headers((unresolvedCall![1] as RequestInit).headers).has("X-Forwarded-Email")).toBe(
      false,
    );

    const pendingResolved = authenticatedFetch(
      "/v1/sessions/sess-wait-resolved/resources/terminals",
    );
    await vi.waitFor(() => expect(resolver).toHaveBeenCalledWith("sess-wait-resolved"));
    releaseSecond();
    await pendingResolved;

    const resolvedCalls = fetcher.mock.calls.filter(
      ([path]) => path === "/v1/sessions/sess-wait-resolved/resources/terminals",
    );
    expect(resolvedCalls).toHaveLength(1);
    expect(new Headers((resolvedCalls[0][1] as RequestInit).headers).get("X-Forwarded-Email")).toBe(
      "alice",
    );
  });

  it("drops the stamped user when the Server changes while the session host resolves", async () => {
    const { setOmnigentHostConfig } = await import("./host");
    const { setSessionHostResolver, resolveIdentity, authenticatedFetch } =
      await import("./identity");

    let releaseResolve!: () => void;
    const resolveGate = new Promise<void>((resolve) => {
      releaseResolve = resolve;
    });
    const resolver = vi.fn().mockReturnValue(resolveGate);
    setSessionHostResolver(resolver);

    const serverA = vi.fn((path: string, init?: RequestInit) => {
      void init;
      if (path === "/v1/me") return Promise.resolve(mockJsonResponse({ user_id: "alice" }));
      return Promise.resolve(mockJsonResponse({}));
    });
    const serverB = vi.fn((path: string, init?: RequestInit) => {
      void init;
      if (path === "/v1/me") return Promise.resolve(mockJsonResponse({ user_id: "bob" }));
      return Promise.resolve(mockJsonResponse({}));
    });
    setOmnigentHostConfig({ serverIdentity: "server-a", fetcher: serverA });
    expect(await resolveIdentity()).toBe("alice");

    const pending = authenticatedFetch("/v1/sessions/sess-switch/resources/terminals");
    await vi.waitFor(() => expect(resolver).toHaveBeenCalledWith("sess-switch"));
    setOmnigentHostConfig({ serverIdentity: "server-b", fetcher: serverB });
    expect(await resolveIdentity()).toBe("bob");
    releaseResolve();
    await pending;

    const allCalls = [...serverA.mock.calls, ...serverB.mock.calls];
    const sessionCalls = allCalls.filter(
      ([path]) => path === "/v1/sessions/sess-switch/resources/terminals",
    );
    expect(sessionCalls).toHaveLength(1);
    const headers = new Headers((sessionCalls[0][1] as RequestInit).headers);
    expect(headers.has("X-Forwarded-Email")).toBe(false);
    expect(headers.get("X-Forwarded-Email")).not.toBe("alice");
  });

  describe("slice-key routing (host sharding)", () => {
    it("stamps X-Databricks-Omnigent-Slice-Key on host-scoped URLs", async () => {
      // Mock sessionHost module before importing identity
      vi.doMock("./sessionHost", () => ({
        getSessionHost: vi.fn(() => "host_123"),
        setSessionHost: vi.fn(),
        isHostKeyless: vi.fn(() => false),
        markHostKeyless: vi.fn(),
        clearHostKeyless: vi.fn(),
        modalHostId: vi.fn(() => null),
        resolveModalHost: vi.fn(),
        isModalHostResolved: vi.fn(() => true),
      }));
      vi.doMock("./host", () => ({
        getOmnigentHostConfig: vi.fn(() => ({ fetcher: () => fetch })),
        getOmnigentHostGeneration: vi.fn(() => 0),
        getOmnigentServerIdentity: vi.fn(() => "server"),
        hostFetch: fetchMock,
        isDatabricksWorkspace: vi.fn(() => true),
      }));

      fetchMock.mockResolvedValueOnce(mockJsonResponse({}));
      const { authenticatedFetch } = await import("./identity");

      await authenticatedFetch("/v1/hosts/host_123/runners");

      const init = fetchMock.mock.calls[0][1] as RequestInit;
      const headers = new Headers(init.headers);
      expect(headers.get("X-Databricks-Omnigent-Slice-Key")).toBe("host_123");
    });

    it("stamps the slice-key header in standalone dev against a workspace", async () => {
      // `npm run dev` pointed at a workspace URL installs no fetcher, but it's
      // still a Databricks workspace (sharded) — the VITE_DATABRICKS_WORKSPACE
      // build flag drives the key so dev traffic reaches the right replica (no
      // manual step).
      vi.stubEnv("VITE_DATABRICKS_WORKSPACE", "true");
      vi.doMock("./sessionHost", () => ({
        getSessionHost: vi.fn(() => null),
        setSessionHost: vi.fn(),
        isHostKeyless: vi.fn(() => false),
        markHostKeyless: vi.fn(),
        clearHostKeyless: vi.fn(),
        modalHostId: vi.fn(() => null),
        resolveModalHost: vi.fn(),
        isModalHostResolved: vi.fn(() => true),
      }));
      vi.doMock("./host", () => ({
        getOmnigentHostConfig: vi.fn(() => ({})),
        getOmnigentHostGeneration: vi.fn(() => 0),
        getOmnigentServerIdentity: vi.fn(() => "server"),
        hostFetch: fetchMock,
        isDatabricksWorkspace: vi.fn(() => true),
      }));

      fetchMock.mockResolvedValueOnce(mockJsonResponse({}));
      const { authenticatedFetch } = await import("./identity");

      await authenticatedFetch("/v1/hosts/host_abc/runners", { method: "POST" });

      const headers = new Headers((fetchMock.mock.calls[0][1] as RequestInit).headers);
      expect(headers.get("X-Databricks-Omnigent-Slice-Key")).toBe("host_abc");
    });

    it("retries keyless on wrong_replica 400 response", async () => {
      vi.doMock("./sessionHost", () => ({
        getSessionHost: vi.fn(() => "host_789"),
        setSessionHost: vi.fn(),
        isHostKeyless: vi.fn(() => false),
        markHostKeyless: vi.fn(),
        clearHostKeyless: vi.fn(),
        modalHostId: vi.fn(() => null),
        resolveModalHost: vi.fn(),
        isModalHostResolved: vi.fn(() => true),
      }));
      vi.doMock("./host", () => ({
        getOmnigentHostConfig: vi.fn(() => ({ fetcher: () => fetch })),
        getOmnigentHostGeneration: vi.fn(() => 0),
        getOmnigentServerIdentity: vi.fn(() => "server"),
        hostFetch: fetchMock,
        isDatabricksWorkspace: vi.fn(() => true),
      }));

      const wrongReplicaResponse = {
        ok: false,
        status: 400,
        statusText: "Bad Request",
        json: async () => ({ error: { code: "wrong_replica" } }),
        clone: function () {
          return this;
        },
      } as unknown as Response;

      fetchMock.mockResolvedValueOnce(wrongReplicaResponse);
      fetchMock.mockResolvedValueOnce(mockJsonResponse({}));

      const { authenticatedFetch } = await import("./identity");
      const response = await authenticatedFetch("/v1/sessions/sess_xyz/resources/terminals");

      // Should retry after the wrong_replica response
      expect(fetchMock).toHaveBeenCalledTimes(2);
      // First call should have the slice key
      const firstInit = fetchMock.mock.calls[0][1] as RequestInit;
      const firstHeaders = new Headers(firstInit.headers);
      expect(firstHeaders.get("X-Databricks-Omnigent-Slice-Key")).toBe("host_789");
      // Second call (retry) should NOT have the slice key
      const secondInit = fetchMock.mock.calls[1][1] as RequestInit;
      const secondHeaders = new Headers(secondInit.headers);
      expect(secondHeaders.get("X-Databricks-Omnigent-Slice-Key")).toBeNull();
      expect(response.status).toBe(200);
    });

    it("keys /v1/imports/local by its body host_id, not the modal host", async () => {
      // The import reads the CHOSEN host's transcripts over that host's tunnel,
      // so it must route to the replica keyed by the body host_id — never the
      // importing user's modal host (a different host, or null for a fresh user),
      // which would land off-replica and 409 "host is not connected".
      vi.doMock("./sessionHost", () => ({
        getSessionHost: vi.fn(() => null),
        setSessionHost: vi.fn(),
        isHostKeyless: vi.fn(() => false),
        markHostKeyless: vi.fn(),
        clearHostKeyless: vi.fn(),
        modalHostId: vi.fn(() => "host_modal"),
        resolveModalHost: vi.fn(),
        isModalHostResolved: vi.fn(() => true),
      }));
      vi.doMock("./host", () => ({
        getOmnigentHostConfig: vi.fn(() => ({ fetcher: () => fetch })),
        getOmnigentHostGeneration: vi.fn(() => 0),
        getOmnigentServerIdentity: vi.fn(() => "server"),
        hostFetch: fetchMock,
        isDatabricksWorkspace: vi.fn(() => true),
      }));

      fetchMock.mockResolvedValueOnce(mockJsonResponse({}));
      const { authenticatedFetch } = await import("./identity");

      await authenticatedFetch("/v1/imports/local/stream", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ host_id: "host_target", source: "all", limit: 5 }),
      });

      const headers = new Headers((fetchMock.mock.calls[0][1] as RequestInit).headers);
      expect(headers.get("X-Databricks-Omnigent-Slice-Key")).toBe("host_target");
    });
  });
});

describe("getCurrentAuthorId", () => {
  it("returns a resolved real identity for self-attribution", async () => {
    fetchMock.mockResolvedValueOnce(mockJsonResponse({ user_id: "alice@example.com" }));
    const { resolveIdentity, getCurrentAuthorId } = await import("./identity");
    await resolveIdentity();
    expect(getCurrentAuthorId()).toBe("alice@example.com");
  });

  it("returns null for the single-user 'local' sentinel", async () => {
    // /v1/me returns "local" when auth is disabled; it is not a distinct
    // actor, so optimistic bubbles must stay unlabeled (no "local" flash).
    fetchMock.mockResolvedValueOnce(mockJsonResponse({ user_id: "local" }));
    const { resolveIdentity, getCurrentAuthorId } = await import("./identity");
    await resolveIdentity();
    expect(getCurrentAuthorId()).toBeNull();
  });

  it("returns null before identity resolves", async () => {
    const { getCurrentAuthorId } = await import("./identity");
    // No resolveIdentity() call: cache is still null, so no label.
    expect(getCurrentAuthorId()).toBeNull();
  });
});

// The login redirect is a once-per-document side effect. An unauthenticated
// page load produces a BURST of 401s (the shell mounts ~8 ungated queries) and
// every one reaches the redirect branch in `authenticatedFetch`. Re-assigning
// `location.href` per failure piled up navigations to a page the browser was
// already headed to; these tests pin it at exactly one.
describe("login redirect", () => {
  const ORIGIN = "https://app.example.com";
  let hrefWrites: string[];
  let originalLocation: Location;

  function stubLocation(pathname: string, search: string) {
    Object.defineProperty(window, "location", {
      configurable: true,
      value: {
        origin: ORIGIN,
        pathname,
        search,
        set href(value: string) {
          hrefWrites.push(value);
        },
        get href() {
          return hrefWrites[hrefWrites.length - 1] ?? `${ORIGIN}${pathname}`;
        },
      },
    });
  }

  beforeEach(() => {
    // The slice-key tests above `vi.doMock("./host")` with a truthy `fetcher`,
    // and doMock registrations outlive `vi.resetModules()`. Left in place, the
    // embedded-host guard short-circuits the entire 401 redirect branch and
    // these tests pass no matter what the code does. Same for ./sessionHost,
    // and for the workspace env stub that turns on slice-key routing.
    vi.doUnmock("./host");
    vi.doUnmock("./sessionHost");
    vi.unstubAllEnvs();

    hrefWrites = [];
    originalLocation = window.location;
    stubLocation("/", "");
  });

  afterEach(() => {
    Object.defineProperty(window, "location", {
      configurable: true,
      value: originalLocation,
    });
  });

  it("redirects once for a whole burst of 401s", async () => {
    // /v1/me 401s with a login page (accounts / OIDC), then eight queries land
    // 401 behind it — the shape of a real logged-out page load.
    fetchMock.mockResolvedValue(
      mockJsonResponse({ user_id: null, login_url: "/login" }, { ok: false, status: 401 }),
    );
    const { resolveIdentity, authenticatedFetch, isLoginRedirectPending } =
      await import("./identity");

    await resolveIdentity();
    await Promise.all(
      Array.from({ length: 8 }, () => authenticatedFetch("/v1/sessions?limit=100")),
    );

    expect(hrefWrites).toEqual(["/login?return_to=%2F"]);
    expect(isLoginRedirectPending()).toBe(true);
  });

  it("preserves the originating path in return_to", async () => {
    stubLocation("/c/abc", "?tab=diff");
    fetchMock.mockResolvedValue(
      mockJsonResponse({ user_id: null, login_url: "/auth/login" }, { ok: false, status: 401 }),
    );
    const { resolveIdentity } = await import("./identity");

    await resolveIdentity();

    expect(hrefWrites).toEqual(["/auth/login?return_to=%2Fc%2Fabc%3Ftab%3Ddiff"]);
  });

  it("never redirects in header mode, where there is no login page", async () => {
    // login_url null → the proxy owns identity, so a stray 401 must surface to
    // the caller instead of bouncing the user to a phantom form.
    fetchMock.mockResolvedValue(mockJsonResponse({ user_id: null }, { ok: false, status: 401 }));
    const { resolveIdentity, authenticatedFetch, isLoginRedirectPending } =
      await import("./identity");

    await resolveIdentity();
    const res = await authenticatedFetch("/v1/hosts");

    expect(hrefWrites).toEqual([]);
    expect(isLoginRedirectPending()).toBe(false);
    expect(res.status).toBe(401);
  });

  it("reports no pending redirect for an authenticated load", async () => {
    fetchMock.mockResolvedValueOnce(mockJsonResponse({ user_id: "alice" }));
    const { resolveIdentity, isLoginRedirectPending } = await import("./identity");

    await resolveIdentity();

    expect(isLoginRedirectPending()).toBe(false);
    expect(hrefWrites).toEqual([]);
  });

  it("discards a stale 401 from before a round trip back to the same Server", async () => {
    let resolveServerA1!: (response: Response) => void;
    const serverA1 = vi.fn().mockReturnValue(
      new Promise<Response>((resolve) => {
        resolveServerA1 = resolve;
      }),
    );
    const serverB = vi.fn().mockResolvedValue(mockJsonResponse({ user_id: "bob" }));
    const serverA2 = vi
      .fn()
      .mockResolvedValue(mockJsonResponse({ user_id: "alice", is_admin: true }));
    const { setOmnigentHostConfig } = await import("./host");
    setOmnigentHostConfig({ serverIdentity: "server-a", fetcher: serverA1 });
    const { resolveIdentity, getCurrentUserId, isLoginRedirectPending } =
      await import("./identity");

    const staleResolution = resolveIdentity();
    setOmnigentHostConfig({ serverIdentity: "server-b", fetcher: serverB });
    expect(await resolveIdentity()).toBe("bob");
    setOmnigentHostConfig({ serverIdentity: "server-a", fetcher: serverA2 });
    expect(await resolveIdentity()).toBe("alice");
    resolveServerA1(
      mockJsonResponse({ user_id: null, login_url: "/login" }, { ok: false, status: 401 }),
    );

    expect(await staleResolution).toBeNull();
    expect(getCurrentUserId()).toBe("alice");
    expect(isLoginRedirectPending()).toBe(false);
    expect(hrefWrites).toEqual([]);
  });
});
