import { beforeEach, describe, expect, it, vi } from "vitest";

import { authenticatedFetch } from "@/lib/identity";
import { fetchWorktreePathTemplate, saveWorktreePathTemplate } from "./worktreeLocationPreference";

vi.mock("@/lib/identity", () => ({ authenticatedFetch: vi.fn() }));

const authenticatedFetchMock = vi.mocked(authenticatedFetch);

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

beforeEach(() => {
  authenticatedFetchMock.mockReset();
});

describe("fetchWorktreePathTemplate", () => {
  it("returns the stored template", async () => {
    authenticatedFetchMock.mockResolvedValue(
      jsonResponse({
        preferences: {
          settings: { worktree_location: { pathTemplate: "{entry}/.worktrees/{repo}/{branch}" } },
        },
      }),
    );

    await expect(fetchWorktreePathTemplate()).resolves.toBe("{entry}/.worktrees/{repo}/{branch}");
    expect(authenticatedFetchMock).toHaveBeenCalledWith("/v1/me", { cache: "no-store" });
  });

  it("returns null when the namespace is absent or malformed", async () => {
    authenticatedFetchMock.mockResolvedValue(
      jsonResponse({ preferences: { settings: { worktree_location: { pathTemplate: 7 } } } }),
    );
    await expect(fetchWorktreePathTemplate()).resolves.toBeNull();

    authenticatedFetchMock.mockResolvedValue(jsonResponse({ preferences: { settings: {} } }));
    await expect(fetchWorktreePathTemplate()).resolves.toBeNull();
  });
});

describe("saveWorktreePathTemplate", () => {
  it("PATCHes the template under the worktree_location namespace", async () => {
    authenticatedFetchMock.mockResolvedValue(jsonResponse({ object: "preferences" }));

    await saveWorktreePathTemplate("{entry}/.worktrees/{repo}/{branch}");

    expect(authenticatedFetchMock).toHaveBeenCalledWith("/v1/me/preferences/worktree_location", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ value: { pathTemplate: "{entry}/.worktrees/{repo}/{branch}" } }),
    });
  });

  it("clears the preference with a null value", async () => {
    authenticatedFetchMock.mockResolvedValue(jsonResponse({ object: "preferences" }));

    await saveWorktreePathTemplate(null);

    expect(JSON.parse(authenticatedFetchMock.mock.calls[0][1]?.body as string)).toEqual({
      value: null,
    });
  });

  it("throws the 422 detail", async () => {
    authenticatedFetchMock.mockResolvedValue(
      jsonResponse({ detail: "worktree location template must contain {branch}" }, 422),
    );

    await expect(saveWorktreePathTemplate("wt/{repo}")).rejects.toThrow(
      "worktree location template must contain {branch}",
    );
  });

  it("throws the error.message shape", async () => {
    authenticatedFetchMock.mockResolvedValue(
      jsonResponse({ error: { message: "Namespace is not writable", code: "forbidden" } }, 403),
    );

    await expect(saveWorktreePathTemplate("wt/{repo}")).rejects.toThrow(
      "Namespace is not writable",
    );
  });

  it("falls back to the status for a non-JSON error body", async () => {
    authenticatedFetchMock.mockResolvedValue(new Response("nope", { status: 500 }));

    await expect(saveWorktreePathTemplate("wt/{repo}")).rejects.toThrow(
      "Saving the worktree location failed (500)",
    );
  });
});
