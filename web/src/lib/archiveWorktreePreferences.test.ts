import { afterEach, describe, expect, it, vi } from "vitest";
import { authenticatedFetch } from "./identity";
import {
  DELETE_WORKTREES_ON_ARCHIVE_STORAGE_KEY,
  fetchArchiveWorktreePreference,
  saveArchiveWorktreePreference,
} from "./archiveWorktreePreferences";

vi.mock("./identity", () => ({ authenticatedFetch: vi.fn(), getCurrentUserId: () => "local" }));

const me = (mode?: string) =>
  Response.json({ preferences: { settings: mode ? { worktree_archive: { mode } } : {} } });

afterEach(() => {
  localStorage.clear();
  vi.restoreAllMocks();
  vi.mocked(authenticatedFetch).mockReset();
});

describe("server archive preference", () => {
  it("defaults to delete_safe without creating a browser preference", async () => {
    vi.mocked(authenticatedFetch).mockResolvedValueOnce(me());
    expect(await fetchArchiveWorktreePreference()).toBe("delete_safe");
    expect(authenticatedFetch).toHaveBeenCalledTimes(1);
  });

  it.each([
    ["omitted preferences", {}],
    ["null settings", { preferences: { settings: null } }],
    ["missing settings", { preferences: {} }],
  ])("keeps %s at never without migrating legacy data", async (_label, payload) => {
    localStorage.setItem(DELETE_WORKTREES_ON_ARCHIVE_STORAGE_KEY, "true");
    vi.mocked(authenticatedFetch).mockResolvedValueOnce(Response.json(payload));
    expect(await fetchArchiveWorktreePreference()).toBe("never");
    expect(authenticatedFetch).toHaveBeenCalledTimes(1);
    expect(localStorage.getItem(DELETE_WORKTREES_ON_ARCHIVE_STORAGE_KEY)).toBe("true");
  });

  it("defaults to delete_safe for a null uninitialized server envelope", async () => {
    vi.mocked(authenticatedFetch).mockResolvedValueOnce(Response.json({ preferences: null }));
    expect(await fetchArchiveWorktreePreference()).toBe("delete_safe");
    expect(authenticatedFetch).toHaveBeenCalledTimes(1);
  });

  it("migrates legacy choice for a null uninitialized server envelope", async () => {
    localStorage.setItem(DELETE_WORKTREES_ON_ARCHIVE_STORAGE_KEY, "false");
    vi.mocked(authenticatedFetch)
      .mockResolvedValueOnce(Response.json({ preferences: null }))
      .mockResolvedValueOnce(Response.json({ mode: "never" }));
    expect(await fetchArchiveWorktreePreference()).toBe("never");
    expect(authenticatedFetch).toHaveBeenCalledTimes(2);
    expect(localStorage.getItem(DELETE_WORKTREES_ON_ARCHIVE_STORAGE_KEY)).toBeNull();
  });

  it.each([null, { mode: "force" }, { mode: null }])(
    "keeps invalid stored choice %j at never without migrating legacy data",
    async (stored) => {
      localStorage.setItem(DELETE_WORKTREES_ON_ARCHIVE_STORAGE_KEY, "true");
      vi.mocked(authenticatedFetch).mockResolvedValueOnce(
        Response.json({ preferences: { settings: { worktree_archive: stored } } }),
      );
      expect(await fetchArchiveWorktreePreference()).toBe("never");
      expect(authenticatedFetch).toHaveBeenCalledTimes(1);
      expect(localStorage.getItem(DELETE_WORKTREES_ON_ARCHIVE_STORAGE_KEY)).toBeNull();
    },
  );

  it.each([
    ["true", "delete_safe"],
    ["false", "never"],
  ] as const)("carries %s over once", async (legacy, mode) => {
    localStorage.setItem(DELETE_WORKTREES_ON_ARCHIVE_STORAGE_KEY, legacy);
    vi.mocked(authenticatedFetch)
      .mockResolvedValueOnce(me())
      .mockResolvedValueOnce(Response.json({ mode }))
      .mockResolvedValueOnce(me(mode));
    expect(await fetchArchiveWorktreePreference()).toBe(mode);
    expect(authenticatedFetch).toHaveBeenNthCalledWith(
      2,
      "/v1/me/preferences/worktree_archive/migrate",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ delete_safe: legacy === "true" }),
      }),
    );
    expect(localStorage.getItem(DELETE_WORKTREES_ON_ARCHIVE_STORAGE_KEY)).toBeNull();
    expect(await fetchArchiveWorktreePreference()).toBe(mode);
    expect(authenticatedFetch).toHaveBeenCalledTimes(3);
  });

  it("keeps an existing server choice when another browser has the old key", async () => {
    localStorage.setItem(DELETE_WORKTREES_ON_ARCHIVE_STORAGE_KEY, "true");
    vi.mocked(authenticatedFetch).mockResolvedValueOnce(me("never"));
    expect(await fetchArchiveWorktreePreference()).toBe("never");
    expect(authenticatedFetch).toHaveBeenCalledTimes(1);
    expect(localStorage.getItem(DELETE_WORKTREES_ON_ARCHIVE_STORAGE_KEY)).toBeNull();
  });

  it("retains the old key after a failed migration", async () => {
    localStorage.setItem(DELETE_WORKTREES_ON_ARCHIVE_STORAGE_KEY, "true");
    vi.mocked(authenticatedFetch)
      .mockResolvedValueOnce(me())
      .mockResolvedValueOnce(new Response(null, { status: 503 }));
    await expect(fetchArchiveWorktreePreference()).rejects.toThrow();
    expect(localStorage.getItem(DELETE_WORKTREES_ON_ARCHIVE_STORAGE_KEY)).toBe("true");
  });

  it("saves the two-value namespace on the server", async () => {
    vi.mocked(authenticatedFetch).mockResolvedValueOnce(Response.json({}));
    await saveArchiveWorktreePreference("delete_safe");
    expect(authenticatedFetch).toHaveBeenCalledWith(
      "/v1/me/preferences/worktree_archive",
      expect.objectContaining({
        method: "PATCH",
        body: JSON.stringify({ value: { mode: "delete_safe" } }),
      }),
    );
    expect(localStorage.getItem(DELETE_WORKTREES_ON_ARCHIVE_STORAGE_KEY)).toBeNull();
  });
});
