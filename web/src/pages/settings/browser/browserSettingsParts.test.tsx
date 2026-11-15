import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { Highlight, SearchPages, ScopeChip } from "./browserSettingsParts";
import { HistoryTab } from "./HistoryTab";
import { BookmarksTab } from "./BookmarksTab";

const notices = vi.hoisted(() => ({ undo: undefined as undefined | (() => void), message: "" }));
vi.mock("sonner", () => ({
  toast: Object.assign(
    (message: string, options?: { action?: { onClick: () => void } }) => {
      if (options?.action) {
        notices.undo = options.action.onClick;
        notices.message = message;
      }
    },
    { error: vi.fn() },
  ),
}));
afterEach(() => {
  cleanup();
  Object.assign(window, { omnigentDesktop: undefined });
  notices.undo = undefined;
  notices.message = "";
});
it("shows scope and keeps literal search matches and clear action accessible", () => {
  const clear = vi.fn();
  render(
    <>
      <ScopeChip />
      <ScopeChip server />
      <Highlight text="example.test/a.b" query="a.b" />
      <SearchPages query="a.b" setQuery={clear} total={30} count={2} kind="history" />
    </>,
  );
  expect(screen.getByText("This desktop")).toBeInTheDocument();
  expect(screen.getByText(/This server ·/)).toBeInTheDocument();
  expect(document.querySelector("mark")).toHaveTextContent("a.b");
  expect(screen.getByText("2 of 30 pages")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Clear search" }));
  expect(clear).toHaveBeenCalledWith("");
});

it.each([
  ["history", "rejects"],
  ["history", "returns an error"],
  ["bookmark", "rejects"],
  ["bookmark", "returns an error"],
])("keeps committed %s deletion Undo when refresh %s", async (kind, failure) => {
  const row = { url: "https://example.test/", title: "Example", visitedAt: Date.now() };
  let deleted = false,
    failRefresh = false;
  const call = vi.fn(async (args: Record<string, unknown>) => {
    if (args.action === "list") {
      if (failRefresh) {
        failRefresh = false;
        if (failure === "rejects") throw Error("IPC closed");
        return { ok: false, error: "Could not read saved pages." };
      }
      return {
        ok: true,
        history: kind === "history" && !deleted ? [row] : [],
        bookmarks: kind === "bookmark" && !deleted ? [row] : [],
      };
    }
    if (args.action === "remove-history" || args.remove) {
      deleted = true;
      failRefresh = true;
      return { ok: true, removed: [row] };
    }
    deleted = false;
    return { ok: true };
  });
  Object.assign(window, {
    omnigentDesktop: {
      browserPlaces: call,
      onBrowserPlaceChosen: () => () => {},
      browserPlacesFeatures: ["remove-history", "update-in-place"],
    },
  });
  render(<MemoryRouter>{kind === "history" ? <HistoryTab /> : <BookmarksTab />}</MemoryRouter>);
  await screen.findByText("Example");
  fireEvent.click(
    screen.getByRole("button", {
      name: kind === "history" ? "Remove page" : "Delete bookmark",
    }),
  );
  await waitFor(() => expect(notices.undo).toBeTypeOf("function"));
  expect(notices.message).toBe(kind === "history" ? "Removed 1 page." : "Bookmark removed.");
  expect(screen.getAllByRole("status").some((node) => node.textContent === "Saved")).toBe(true);
  expect(screen.getByRole("alert")).toHaveTextContent("Could not refresh saved pages.");
  expect(screen.queryByText("Could not save changes.")).not.toBeInTheDocument();
  notices.undo?.();
  await waitFor(() =>
    expect(call).toHaveBeenCalledWith(
      kind === "history"
        ? { action: "restore-history", rows: [row] }
        : { action: "update", url: row.url, title: row.title, index: 0 },
    ),
  );
  await waitFor(() => expect(screen.queryByRole("alert")).not.toBeInTheDocument());
});
