import { QueryClient } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  cachedProjectSummaries,
  createProjectSummary,
  listProjectSummaries,
  parseCreateProjectParams,
  projectSummary,
} from "./projects";

const { authenticatedFetchMock } = vi.hoisted(() => ({ authenticatedFetchMock: vi.fn() }));
vi.mock("@/lib/identity", () => ({ authenticatedFetch: authenticatedFetchMock }));

beforeEach(() => authenticatedFetchMock.mockReset());

describe("projectSummary", () => {
  it("keeps only id, name, and the whole stored icon", () => {
    const summary = projectSummary({
      id: "proj_1",
      name: "n".repeat(300),
      user_id: "private@example.com",
      created_at: 1,
      config: { icon: "tabler:device-ipad-horizontal-exclamation" },
    });
    expect(Object.keys(summary).sort()).toEqual(["icon", "id", "name"]);
    expect(summary.name).toHaveLength(100);
    // 41 characters; the longest installed Tabler id, untouched by the summary.
    expect(summary.icon).toBe("tabler:device-ipad-horizontal-exclamation");
    expect(projectSummary({ id: "proj_2", name: "Plain" }).icon).toBeNull();
  });

  it("passes a stored icon through whole", () => {
    expect(
      projectSummary({ id: "p", name: "P", config: { icon: "tabler:brand-github" } }).icon,
    ).toBe("tabler:brand-github");
    // A multi-codepoint Emoji sequence used to be sliced at 16 UTF-16 units.
    const sequence = "\u{1F468}\u200D\u{1F469}\u200D\u{1F467}\u200D\u{1F466}".repeat(2);
    expect(sequence.length).toBeGreaterThan(16);
    expect(projectSummary({ id: "p", name: "P", config: { icon: sequence } }).icon).toBe(sequence);
  });
});

describe("cachedProjectSummaries", () => {
  it("reuses first-class sidebar projects and ignores legacy label folders", () => {
    const queryClient = new QueryClient();
    queryClient.setQueryData(
      ["projects"],
      [
        { id: "proj_1", name: "Alpha", icon: "🅰️" },
        { id: null, name: "Legacy", icon: null },
      ],
    );

    expect(cachedProjectSummaries(queryClient)).toEqual([
      { id: "proj_1", name: "Alpha", icon: "🅰️" },
    ]);
  });

  it("keeps a cached Tabler icon whole", () => {
    const queryClient = new QueryClient();
    queryClient.setQueryData(
      ["projects"],
      [{ id: "proj_1", name: "Alpha", icon: "tabler:brand-google-play" }],
    );

    expect(cachedProjectSummaries(queryClient)).toEqual([
      { id: "proj_1", name: "Alpha", icon: "tabler:brand-google-play" },
    ]);
  });
});

describe("parseCreateProjectParams", () => {
  it("trims the name and rejects empty or oversized names", () => {
    expect(parseCreateProjectParams({ name: "  Alpha  " })).toBe("Alpha");
    for (const params of [null, [], {}, { name: 3 }, { name: "   " }, { name: "x".repeat(101) }]) {
      expect(() => parseCreateProjectParams(params)).toThrow(
        expect.objectContaining({ code: "InvalidParams" }),
      );
    }
  });
});

describe("project host requests", () => {
  it("lists projects through the projects API", async () => {
    authenticatedFetchMock.mockResolvedValue(
      new Response(
        JSON.stringify({
          object: "list",
          data: [
            { id: "p1", name: "Alpha", config: { icon: "🅰️" } },
            { id: "p2", name: "Beta" },
          ],
        }),
        { status: 200 },
      ),
    );
    await expect(listProjectSummaries()).resolves.toEqual([
      { id: "p1", name: "Alpha", icon: "🅰️" },
      { id: "p2", name: "Beta", icon: null },
    ]);
    expect(authenticatedFetchMock).toHaveBeenCalledWith("/v1/projects");
  });

  it("posts the trimmed name and surfaces the server's rejection", async () => {
    authenticatedFetchMock.mockResolvedValueOnce(
      new Response(JSON.stringify({ id: "p3", name: "Gamma", config: {} }), { status: 200 }),
    );
    await expect(createProjectSummary("Gamma")).resolves.toEqual({
      id: "p3",
      name: "Gamma",
      icon: null,
    });
    const [url, init] = authenticatedFetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/v1/projects");
    expect(init.method).toBe("POST");
    expect(JSON.parse(String(init.body))).toEqual({ name: "Gamma" });

    authenticatedFetchMock.mockResolvedValueOnce(
      new Response(JSON.stringify({ error: { message: "name must not be empty" } }), {
        status: 400,
      }),
    );
    await expect(createProjectSummary("Delta")).rejects.toMatchObject({
      code: "HostError",
      message: "Project could not be created: name must not be empty",
    });
  });
});
