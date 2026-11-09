import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { BrowserPlacesSettings } from "./BrowserPlacesSettings";

afterEach(() => {
  cleanup();
  (window as unknown as { omnigentDesktop?: unknown }).omnigentDesktop = undefined;
});
it("manages local bookmarks and confirms history clear, reporting write failures", async () => {
  let rows = [{ url: "https://example.test", title: "Example" }];
  let history = [...rows];
  let fail = true;
  const call = vi.fn(async (args: Record<string, unknown>) => {
    if (args.action === "list") return { ok: true, bookmarks: rows, history };
    if (args.action === "update") {
      if (fail) return { ok: false, error: "Could not save bookmark." };
      rows = args.remove ? [] : [{ url: "https://example.test", title: String(args.title) }];
    }
    if (args.action === "clear-history") history = [];
    return { ok: true };
  });
  (window as unknown as { omnigentDesktop?: unknown }).omnigentDesktop = {
    browserPlaces: call,
    onBrowserPlaceChosen: () => () => {},
  };
  render(<BrowserPlacesSettings />);
  const name = await screen.findByRole("textbox", { name: /Bookmark name/ });
  fireEvent.change(name, { target: { value: "Renamed" } });
  fireEvent.click(screen.getByRole("button", { name: "Rename" }));
  await waitFor(() =>
    expect(screen.getByRole("status")).toHaveTextContent("Could not save bookmark."),
  );
  fail = false;
  fireEvent.click(screen.getByRole("button", { name: "Rename" }));
  await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("Bookmark renamed."));
  fireEvent.click(screen.getByRole("button", { name: "Clear history" }));
  expect(call.mock.calls.some(([args]) => args.action === "clear-history")).toBe(false);
  fireEvent.click(screen.getByRole("button", { name: "Clear all history" }));
  await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("History cleared."));
  expect(screen.getByText("History (0)")).toBeInTheDocument();
});
