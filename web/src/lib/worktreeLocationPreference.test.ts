import { beforeEach, describe, expect, it, vi } from "vitest";

import { authenticatedFetch } from "@/lib/identity";
import { fetchWorktreePathTemplate } from "./worktreeLocationPreference";

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
