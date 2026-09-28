// Lookup behaviour of the selection popup's file resolver.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { FileViewerContext } from "@/shell/FileViewerContext";
import { useSelectionFileMatch, type SelectionFileMatchResult } from "./useSelectionFileMatch";

const fetchMock = vi.fn();

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function jsonResponse(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: status === 200 ? "OK" : "Error",
    json: async () => body,
  } as unknown as Response;
}

function listing(paths: string[], truncated = false): Response {
  return jsonResponse({
    object: "list",
    data: paths.map((path) => ({
      id: path,
      name: path.split("/").pop(),
      path,
      type: "file",
      bytes: 10,
      modified_at: 1,
    })),
    // Directory listings report `has_more`; the search endpoint `truncated`.
    has_more: truncated,
    truncated,
  });
}

const WORKSPACE = "/home/u/ws";

// Module scope so the provider value is a constant (jsx-no-constructed-context-values).
const VIEWER = {
  openFile: () => {},
  openGithubTab: () => {},
  isChangedPath: () => false,
  conversationId: undefined as string | undefined,
  workspaceRoot: WORKSPACE,
  workspaceHome: "/home/u",
};

function Probe({ id, text }: { id: string | undefined; text: string }) {
  const result = useSelectionFileMatch(id, text);
  return <output data-testid="result">{JSON.stringify(result)}</output>;
}

function renderProbe(text: string, id: string | undefined = "conv_1") {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  return render(
    <QueryClientProvider client={client}>
      <FileViewerContext.Provider value={VIEWER}>
        <Probe id={id} text={text} />
      </FileViewerContext.Provider>
    </QueryClientProvider>,
  );
}

function readResult(): SelectionFileMatchResult {
  return JSON.parse(screen.getByTestId("result").textContent ?? "{}") as SelectionFileMatchResult;
}

async function settled(): Promise<SelectionFileMatchResult> {
  await waitFor(() => expect(readResult().pending).toBe(false));
  return readResult();
}

describe("useSelectionFileMatch", () => {
  it("confirms an exact relative path through its parent listing", async () => {
    fetchMock.mockResolvedValue(listing(["report/page2.html"]));

    renderProbe("report/page2.html");

    expect((await settled()).matches).toEqual([{ path: "report/page2.html", line: null }]);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0][0]).toContain("/filesystem/report?");
  });

  it("keeps the cited line of the selection", async () => {
    fetchMock.mockResolvedValue(listing(["report/page2.html"]));

    renderProbe("report/page2.html:12");

    expect((await settled()).matches).toEqual([{ path: "report/page2.html", line: 12 }]);
  });

  it("finds a unique basename through the server search", async () => {
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/search")) {
        expect(url).toContain("q=page3.html");
        return listing(["report/sub/page3.html"]);
      }
      return listing([]);
    });

    renderProbe("page3.html");

    expect((await settled()).matches).toEqual([{ path: "report/sub/page3.html", line: null }]);
  });

  it("matches a selection that is the tail of a longer basename", async () => {
    const fullName = "weekly_chat-link-panel_report.html";
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      if (String(input).includes("/search")) return listing([fullName]);
      return listing([]);
    });

    renderProbe("link-panel_report.html");

    expect((await settled()).matches).toEqual([{ path: fullName, line: null }]);
  });

  it("requires a slash candidate to match the result's tail", async () => {
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      if (String(input).includes("/search")) {
        return listing(["sub/report/page2.html", "sub/report/page2.html.bak"]);
      }
      return listing(["report/other.html"]);
    });

    renderProbe("report/page2.html");

    expect((await settled()).matches).toEqual([{ path: "sub/report/page2.html", line: null }]);
  });

  it("caps the list and floats exact basename matches first", async () => {
    const searchHits = [
      "s1/index.html.bak",
      "s2/myindex.html",
      "z/index.html",
      "y/index.html",
      "s3/other-index.html",
      "s4/index.html.tmp",
      "s5/index.html.save",
      "s6/xindex.html",
      "x/index.html",
      "s7/index.html.old",
    ];
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      if (String(input).includes("/search")) return listing(searchHits);
      return listing([]);
    });

    renderProbe("index.html");
    const result = await settled();

    expect(result.matches).toHaveLength(8);
    expect(result.matches.slice(0, 3).map((m) => m.path)).toEqual([
      "z/index.html",
      "y/index.html",
      "x/index.html",
    ]);
  });

  it("falls through a truncated exact listing to the search", async () => {
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      if (String(input).includes("/search")) return listing(["deep/report/page2.html"]);
      return listing(["report/other.html"], true);
    });

    renderProbe("report/page2.html");

    expect((await settled()).matches).toEqual([{ path: "deep/report/page2.html", line: null }]);
  });

  it("reports a truncated search as an incomplete result", async () => {
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      if (String(input).includes("/search")) return listing(["report/index.html"], true);
      return listing([]);
    });

    renderProbe("index.html");
    const result = await settled();

    expect(result.truncated).toBe(true);
    expect(result.matches).toHaveLength(1);
  });

  it("settles empty when the runner is unavailable", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ error: { code: "runner_unavailable" } }, 503));

    renderProbe("report/page2.html");
    const result = await settled();

    expect(result.matches).toEqual([]);
    expect(result.truncated).toBe(false);
  });

  it("shares one parent listing between callers with the same selection", async () => {
    fetchMock.mockResolvedValue(listing(["report/page2.html"]));
    const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
    render(
      <QueryClientProvider client={client}>
        <FileViewerContext.Provider value={VIEWER}>
          <Probe id="conv_1" text="report/page2.html" />
          <Probe id="conv_1" text="report/page2.html" />
        </FileViewerContext.Provider>
      </QueryClientProvider>,
    );

    await waitFor(() => expect(screen.getAllByTestId("result")).toHaveLength(2));
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("does not look up prose or oversized selections", async () => {
    const prose = ["the quick fix", "npm run build", "x".repeat(301), "two\nlines.md"];
    for (const text of prose) {
      const { unmount } = renderProbe(text);
      expect(readResult()).toEqual({ matches: [], pending: false, truncated: false });
      unmount();
    }
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
