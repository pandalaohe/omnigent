import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { HistoryTab } from "./HistoryTab";

const notices = vi.hoisted(() => ({ undo: undefined as undefined | (() => void) }));
vi.mock("sonner", () => ({
  toast: Object.assign(
    (_message: string, options?: { action?: { onClick: () => void } }) => {
      notices.undo = options?.action?.onClick;
    },
    { error: vi.fn() },
  ),
}));
afterEach(() => {
  cleanup();
  Object.assign(window, { omnigentDesktop: undefined });
});
it("commits a row removal before Undo and restores the returned rows", async () => {
  const row = { url: "https://example.test/", title: "Example", visitedAt: Date.now() };
  let rows = [row];
  const call = vi.fn(async (args: Record<string, unknown>) => {
    if (args.action === "remove-history") {
      rows = [];
      return { ok: true, removed: [row] };
    }
    if (args.action === "restore-history") rows = [row];
    return { ok: true, history: rows, bookmarks: [] };
  });
  Object.assign(window, {
    omnigentDesktop: {
      browserPlaces: call,
      onBrowserPlaceChosen: () => () => {},
      browserPlacesFeatures: ["remove-history"],
    },
  });
  render(
    <MemoryRouter>
      <HistoryTab />
    </MemoryRouter>,
  );
  await screen.findByText("Example");
  fireEvent.click(screen.getByRole("button", { name: "Remove page" }));
  await waitFor(() => expect(screen.queryByText("Example")).not.toBeInTheDocument());
  notices.undo?.();
  await screen.findByText("Example");
  expect(call).toHaveBeenCalledWith({ action: "restore-history", rows: [row] });
});
it("gates each capability independently on a shell with retention only", async () => {
  const call = vi.fn(async (args: Record<string, unknown>) =>
    args.action === "retention"
      ? { ok: true, days: 45, entries: 1500 }
      : { ok: true, history: [{ url: "https://example.test/", title: "Example" }], bookmarks: [] },
  );
  Object.assign(window, {
    omnigentDesktop: {
      browserPlaces: call,
      onBrowserPlaceChosen: () => () => {},
      browserPlacesFeatures: ["retention"],
    },
  });
  render(
    <MemoryRouter>
      <HistoryTab />
    </MemoryRouter>,
  );
  await screen.findByText("Example");
  expect(screen.queryByRole("button", { name: "Remove page" })).not.toBeInTheDocument();
  expect(screen.getByRole("combobox", { name: "Keep pages for" })).not.toBeDisabled();
  expect(await screen.findByText("Custom (45 days)")).toBeInTheDocument();
  expect(
    call.mock.calls.every(([args]) => ["list", "retention"].includes(args.action as string)),
  ).toBe(true);
});
