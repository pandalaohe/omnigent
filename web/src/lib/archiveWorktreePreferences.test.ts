import { afterEach, describe, expect, it, vi } from "vitest";
import { authenticatedFetch } from "./identity";
import {
  DELETE_WORKTREES_ON_ARCHIVE_STORAGE_KEY,
  DEFAULT_REBUILDABLE_PATHS,
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
  it("defaults to never and asks on first archive without creating a browser preference", async () => {
    vi.mocked(authenticatedFetch).mockResolvedValueOnce(me());
    expect(await fetchArchiveWorktreePreference()).toEqual({
      mode: "never",
      ask_before_archive: true,
      rebuildable_paths: DEFAULT_REBUILDABLE_PATHS,
    });
    expect(authenticatedFetch).toHaveBeenCalledTimes(1);
  });

  it.each([
    ["omitted preferences", {}],
    ["null settings", { preferences: { settings: null } }],
    ["missing settings", { preferences: {} }],
  ])("keeps %s at never without migrating legacy data", async (_label, payload) => {
    localStorage.setItem(DELETE_WORKTREES_ON_ARCHIVE_STORAGE_KEY, "true");
    vi.mocked(authenticatedFetch).mockResolvedValueOnce(Response.json(payload));
    expect(await fetchArchiveWorktreePreference()).toMatchObject({
      mode: "never",
      ask_before_archive: false,
    });
    expect(authenticatedFetch).toHaveBeenCalledTimes(1);
    expect(localStorage.getItem(DELETE_WORKTREES_ON_ARCHIVE_STORAGE_KEY)).toBe("true");
  });

  it("defaults to never and asks for a null uninitialized server envelope", async () => {
    vi.mocked(authenticatedFetch).mockResolvedValueOnce(Response.json({ preferences: null }));
    expect(await fetchArchiveWorktreePreference()).toMatchObject({
      mode: "never",
      ask_before_archive: true,
    });
    expect(authenticatedFetch).toHaveBeenCalledTimes(1);
  });

  it("migrates legacy choice for a null uninitialized server envelope", async () => {
    localStorage.setItem(DELETE_WORKTREES_ON_ARCHIVE_STORAGE_KEY, "false");
    vi.mocked(authenticatedFetch)
      .mockResolvedValueOnce(Response.json({ preferences: null }))
      .mockResolvedValueOnce(Response.json({ mode: "never" }));
    expect(await fetchArchiveWorktreePreference()).toMatchObject({
      mode: "never",
      ask_before_archive: false,
    });
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
      expect(await fetchArchiveWorktreePreference()).toMatchObject({
        mode: "never",
        ask_before_archive: false,
      });
      expect(authenticatedFetch).toHaveBeenCalledTimes(1);
      expect(localStorage.getItem(DELETE_WORKTREES_ON_ARCHIVE_STORAGE_KEY)).toBeNull();
    },
  );

  it.each([
    ["true", "green_only"],
    ["false", "never"],
  ] as const)("carries %s over once", async (legacy, mode) => {
    localStorage.setItem(DELETE_WORKTREES_ON_ARCHIVE_STORAGE_KEY, legacy);
    vi.mocked(authenticatedFetch)
      .mockResolvedValueOnce(me())
      .mockResolvedValueOnce(
        Response.json({
          mode,
          ask_before_archive: legacy === "true",
          rebuildable_paths: DEFAULT_REBUILDABLE_PATHS,
        }),
      )
      .mockResolvedValueOnce(
        Response.json({
          preferences: {
            settings: {
              worktree_archive: {
                mode,
                ask_before_archive: legacy === "true",
                rebuildable_paths: DEFAULT_REBUILDABLE_PATHS,
              },
            },
          },
        }),
      );
    expect(await fetchArchiveWorktreePreference()).toMatchObject({
      mode,
      ask_before_archive: legacy === "true",
    });
    expect(authenticatedFetch).toHaveBeenNthCalledWith(
      2,
      "/v1/me/preferences/worktree_archive/migrate",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ delete_safe: legacy === "true" }),
      }),
    );
    expect(localStorage.getItem(DELETE_WORKTREES_ON_ARCHIVE_STORAGE_KEY)).toBeNull();
    expect(await fetchArchiveWorktreePreference()).toMatchObject({
      mode,
      ask_before_archive: legacy === "true",
    });
    expect(authenticatedFetch).toHaveBeenCalledTimes(3);
  });

  it("keeps an existing server choice when another browser has the old key", async () => {
    localStorage.setItem(DELETE_WORKTREES_ON_ARCHIVE_STORAGE_KEY, "true");
    vi.mocked(authenticatedFetch).mockResolvedValueOnce(me("never"));
    expect(await fetchArchiveWorktreePreference()).toMatchObject({
      mode: "never",
      ask_before_archive: false,
    });
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

  it("saves the tier, prompt choice and rebuildable list together", async () => {
    vi.mocked(authenticatedFetch).mockResolvedValueOnce(Response.json({}));
    const value = {
      mode: "yellow_too" as const,
      ask_before_archive: false,
      rebuildable_paths: ["dist/"],
    };
    await saveArchiveWorktreePreference(value);
    expect(authenticatedFetch).toHaveBeenCalledWith(
      "/v1/me/preferences/worktree_archive",
      expect.objectContaining({
        method: "PATCH",
        body: JSON.stringify({ value }),
      }),
    );
    expect(localStorage.getItem(DELETE_WORKTREES_ON_ARCHIVE_STORAGE_KEY)).toBeNull();
  });

  it("maps old delete_safe to green only and asks once for the broader policy", async () => {
    vi.mocked(authenticatedFetch).mockResolvedValueOnce(me("delete_safe"));
    expect(await fetchArchiveWorktreePreference()).toEqual({
      mode: "green_only",
      ask_before_archive: true,
      rebuildable_paths: DEFAULT_REBUILDABLE_PATHS,
    });
  });

  it.each(["never", "green_only", "yellow_too"] as const)(
    "reads the saved %s tier and custom list",
    async (mode) => {
      const value = { mode, ask_before_archive: true, rebuildable_paths: ["tmp/build/"] };
      vi.mocked(authenticatedFetch).mockResolvedValueOnce(
        Response.json({ preferences: { settings: { worktree_archive: value } } }),
      );
      expect(await fetchArchiveWorktreePreference()).toEqual(value);
    },
  );

  it.each([
    { mode: "yellow_too", ask_before_archive: "false", rebuildable_paths: [] },
    { mode: "yellow_too", ask_before_archive: false, rebuildable_paths: [1] },
    { mode: "green_only", ask_before_archive: false },
  ])("keeps malformed settings at never (%j)", async (value) => {
    vi.mocked(authenticatedFetch).mockResolvedValueOnce(
      Response.json({ preferences: { settings: { worktree_archive: value } } }),
    );
    expect(await fetchArchiveWorktreePreference()).toMatchObject({
      mode: "never",
      ask_before_archive: false,
    });
  });
});
