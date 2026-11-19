import { fireEvent, render, screen } from "@testing-library/react";
import { Link, MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

const catalog = vi.hoisted(() => ({ loading: true, extensions: [] as unknown[] }));

vi.mock("./ExtensionProvider", () => ({
  useExtensions: () => catalog.extensions,
  useExtensionsLoading: () => catalog.loading,
}));
vi.mock("@/pages/NotFoundPage", () => ({
  NotFoundPage: () => <div>page not found</div>,
}));
vi.mock("./ExtensionPageHost", () => ({
  ExtensionPageHost: ({ hash }: { hash: string }) => <div data-testid="extension-hash">{hash}</div>,
}));

import { ExtensionPageRoute } from "./ExtensionPageRoute";

function renderRoute() {
  return render(
    <MemoryRouter initialEntries={["/extensions/acme.review/dashboard#/first"]}>
      <Link to="#/second">Second view</Link>
      <Link to="/extensions/acme.review/dashboard">Default view</Link>
      <Routes>
        <Route path="/extensions/:extensionId/*" element={<ExtensionPageRoute />} />
      </Routes>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  catalog.loading = true;
  catalog.extensions = [];
});

describe("ExtensionPageRoute", () => {
  it("shows only a spinner while the extension catalog is unresolved", () => {
    renderRoute();

    expect(screen.getByRole("status", { name: "Loading extension" })).toBeInTheDocument();
    expect(screen.queryByText("page not found")).toBeNull();
  });

  it("shows not found after the extension catalog resolves", () => {
    catalog.loading = false;
    renderRoute();

    expect(screen.getByText("page not found")).toBeInTheDocument();
    expect(screen.queryByRole("status")).toBeNull();
  });

  it("passes initial, changed and cleared route fragments to the same page host", () => {
    catalog.loading = false;
    catalog.extensions = [{ id: "acme.review", pages: [{ route: "dashboard" }] }];
    renderRoute();
    const target = screen.getByTestId("extension-hash");
    expect(target).toHaveTextContent("#/first");
    fireEvent.click(screen.getByRole("link", { name: "Second view" }));
    expect(target).toHaveTextContent("#/second");
    fireEvent.click(screen.getByRole("link", { name: "Default view" }));
    expect(target).toBeEmptyDOMElement();
  });
});
