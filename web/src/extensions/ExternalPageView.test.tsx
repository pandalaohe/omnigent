import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ExtensionCatalogItem, ExtensionExternalPage, ExtensionViewPage } from "./types";

const { viewHostMock } = vi.hoisted(() => ({ viewHostMock: vi.fn() }));
vi.mock("./ExtensionProvider", () => ({ useRefreshExtensions: () => vi.fn() }));
vi.mock("./services/useExtensionHostServices", () => ({
  useExtensionHostServices: () => ({ methods: {}, events: {} }),
}));
vi.mock("./ExtensionViewHost", () => ({ ExtensionViewHost: viewHostMock }));
vi.mock("@/pages/NotFoundPage", () => ({
  NotFoundPage: () => <div>Page not found</div>,
}));

import { ExternalPageView } from "./ExternalPageView";
import { ExtensionPageHost } from "./ExtensionPageHost";

const page: ExtensionExternalPage = {
  id: "acme.dashboard.overview",
  title: "Dashboard",
  route: "overview",
  kind: "external",
  view: null,
  url: "https://dashboard.example.com/",
};
const extension = { id: "acme.dashboard" } as ExtensionCatalogItem;

beforeEach(() => {
  vi.stubGlobal("location", new URL("http://app.example.com/"));
  viewHostMock.mockReset().mockReturnValue(<div>Browser view</div>);
});

afterEach(() => {
  delete window.__OMNIGENT_BASE_PATH__;
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("ExternalPageView", () => {
  it("prefixes proxy frame, popup and tab URLs for a subpath deployment", () => {
    window.__OMNIGENT_BASE_PATH__ = "/proxy/6767";
    const open = vi.spyOn(window, "open").mockReturnValue(null);
    const url = "/v1/extensions/acme.dashboard/proxy/overview/?view=mini#details";
    render(<ExternalPageView page={{ ...page, kind: "proxied", url }} />);
    const expected = "/proxy/6767" + url;
    expect(screen.getByTitle("Dashboard")).toHaveAttribute("src", expected);
    expect(screen.getByRole("link", { name: "Open in new tab" })).toHaveAttribute("href", expected);
    fireEvent.click(screen.getByRole("button", { name: "Open in window" }));
    expect(open).toHaveBeenCalledExactlyOnceWith(
      expected,
      "_blank",
      "popup,width=480,height=720,noopener,noreferrer",
    );
  });
  it("frames a server proxy and keeps standalone navigation behind the same route", () => {
    vi.stubGlobal("location", new URL("https://app.example.com/"));
    const url = "/v1/extensions/acme.dashboard/proxy/overview/?view=mini#details";
    render(<ExternalPageView page={{ ...page, kind: "proxied", url }} />);
    expect(screen.getByTitle("Dashboard")).toHaveAttribute("src", url);
    expect(screen.getByRole("link", { name: "Open in new tab" })).toHaveAttribute("href", url);
  });

  it.each([
    "https://other.example.com/",
    "/v1/sessions/",
    "//other.example.com/",
    "/v1/extensions/acme.dashboard/proxy/overview/../other/",
  ])("refuses a proxied page outside its server route: %s", (url) => {
    render(<ExternalPageView page={{ ...page, kind: "proxied", url }} />);
    expect(screen.getByRole("alert")).toBeInTheDocument();
    expect(screen.queryByTitle("Dashboard")).toBeNull();
  });
  it("frames the declared URL with a fixed sandbox and no referrer or device permissions", () => {
    render(<ExternalPageView page={page} />);

    const frame = screen.getByTitle("Dashboard");
    expect(frame).toHaveAttribute("src", "https://dashboard.example.com/");
    expect(frame).not.toHaveAttribute("srcdoc");
    expect(frame).toHaveAttribute(
      "sandbox",
      "allow-scripts allow-same-origin allow-forms allow-popups allow-popups-to-escape-sandbox",
    );
    expect(frame).toHaveAttribute("referrerpolicy", "no-referrer");
    expect(frame).toHaveAttribute("allow", "");
    expect(frame).toHaveClass("min-h-0", "flex-1");
    expect(screen.getByRole("heading", { name: "Dashboard" })).toBeInTheDocument();
    const link = screen.getByRole("link", { name: "Open in new tab" });
    expect(link).toHaveAttribute("href", "https://dashboard.example.com/");
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
  });

  it("requests a standalone popup with no opener or referrer", () => {
    const open = vi.spyOn(window, "open").mockReturnValue(null);
    render(<ExternalPageView page={page} />);

    fireEvent.click(screen.getByRole("button", { name: "Open in window" }));

    expect(open).toHaveBeenCalledExactlyOnceWith(
      "https://dashboard.example.com/",
      "_blank",
      "popup,width=480,height=720,noopener,noreferrer",
    );
  });

  it.each(["not a url", "/reports", "javascript:alert(1)", "ftp://dashboard.example.com/"])(
    "refuses invalid catalog URL %s without offering navigation",
    (url) => {
      render(<ExternalPageView page={{ ...page, url }} />);

      expect(screen.getByRole("alert")).toHaveTextContent("Dashboard cannot be shown");
      expect(screen.queryByTitle("Dashboard")).toBeNull();
      expect(screen.queryByRole("button")).toBeNull();
      expect(screen.queryByRole("link")).toBeNull();
    },
  );

  it("refuses the shell's own origin without offering navigation", () => {
    render(<ExternalPageView page={{ ...page, url: "http://app.example.com/reports" }} />);

    expect(screen.getByRole("alert")).toHaveTextContent("application's own origin");
    expect(screen.queryByTitle("Dashboard")).toBeNull();
    expect(screen.queryByRole("button")).toBeNull();
    expect(screen.queryByRole("link")).toBeNull();
  });

  it.each(["http://dashboard.example.com/", "http://localhost:9000/"])(
    "blocks mixed content %s while keeping both navigation options",
    (url) => {
      vi.stubGlobal("location", new URL("https://app.example.com/"));
      const open = vi.spyOn(window, "open").mockReturnValue(null);
      render(<ExternalPageView page={{ ...page, url }} />);

      expect(
        screen.getByText(/HTTP page cannot be embedded in an HTTPS application/),
      ).toBeVisible();
      expect(screen.queryByTitle("Dashboard")).toBeNull();
      expect(screen.getByRole("link", { name: "Open in new tab" })).toHaveAttribute("href", url);
      fireEvent.click(screen.getByRole("button", { name: "Open in window" }));
      expect(open).toHaveBeenCalledExactlyOnceWith(
        url,
        "_blank",
        "popup,width=480,height=720,noopener,noreferrer",
      );
    },
  );

  it.each([
    ["http://app.example.com/", "http://dashboard.example.com/"],
    ["https://app.example.com/", "https://dashboard.example.com/"],
  ])("frames compatible protocols from %s to %s", (shell, url) => {
    vi.stubGlobal("location", new URL(shell));
    render(<ExternalPageView page={{ ...page, url }} />);

    expect(screen.getByTitle("Dashboard")).toHaveAttribute("src", url);
    expect(screen.queryByRole("alert")).toBeNull();
  });
});

describe("ExtensionPageHost", () => {
  it("routes a proxied page without mounting the bundle host", () => {
    const url = "/v1/extensions/acme.dashboard/proxy/overview/";
    render(<ExtensionPageHost resolved={{ extension, page: { ...page, kind: "proxied", url } }} />);
    expect(screen.getByTitle("Dashboard")).toHaveAttribute("src", url);
    expect(viewHostMock).not.toHaveBeenCalled();
  });
  it("routes external pages without mounting the bundle host", () => {
    render(<ExtensionPageHost resolved={{ extension, page }} />);

    expect(screen.getByTitle("Dashboard")).toHaveAttribute("src", "https://dashboard.example.com/");
    expect(viewHostMock).not.toHaveBeenCalled();
  });

  it.each([undefined, "view"] as const)("routes view pages with kind %s", (kind) => {
    const viewPage: ExtensionViewPage = {
      id: "acme.dashboard.overview",
      title: "Dashboard",
      route: "overview",
      view: "dashboard",
      ...(kind === undefined ? {} : { kind }),
    };
    render(<ExtensionPageHost resolved={{ extension, page: viewPage }} />);

    expect(screen.getByText("Browser view")).toBeInTheDocument();
    expect(viewHostMock).toHaveBeenCalledOnce();
    expect(viewHostMock.mock.calls[0][0]).toEqual(
      expect.objectContaining({ extension, page: viewPage }),
    );
    expect(screen.queryByTitle("Dashboard")).toBeNull();
  });

  it("routes unknown catalog kinds to not found without mounting the bundle host", () => {
    const unknownPage = { ...page, kind: "widget" } as unknown as ExtensionExternalPage;
    render(<ExtensionPageHost resolved={{ extension, page: unknownPage }} />);

    expect(screen.getByText("Page not found")).toBeInTheDocument();
    expect(viewHostMock).not.toHaveBeenCalled();
    expect(screen.queryByTitle("Dashboard")).toBeNull();
  });

  it("refuses invalid external catalog URLs without mounting the bundle host", () => {
    render(<ExtensionPageHost resolved={{ extension, page: { ...page, url: "not a url" } }} />);

    expect(screen.getByRole("alert")).toHaveTextContent("Dashboard cannot be shown");
    expect(viewHostMock).not.toHaveBeenCalled();
    expect(screen.queryByTitle("Dashboard")).toBeNull();
    expect(screen.queryByRole("button")).toBeNull();
    expect(screen.queryByRole("link")).toBeNull();
  });
});
