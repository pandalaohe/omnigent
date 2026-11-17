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
  await waitFor(() =>
    expect(screen.getByRole("combobox", { name: "Keep pages for" })).toHaveValue(
      "Custom (45 days)",
    ),
  );
  expect(
    call.mock.calls.every(([args]) => ["list", "retention"].includes(args.action as string)),
  ).toBe(true);
});

it("confirms the range and count before clearing and keeps Cancel usable at zero", async () => {
  let history = [
    { url: "https://example.test/", title: "Example", visitedAt: Date.now() - 2 * 3600000 },
  ];
  const call = vi.fn(async (args: Record<string, unknown>) => {
    if (args.action === "clear-history") history = [];
    return { ok: true, history, bookmarks: [] };
  });
  Object.assign(window, {
    omnigentDesktop: {
      browserPlaces: call,
      onBrowserPlaceChosen: () => () => {},
      browserPlacesFeatures: ["clear-since"],
    },
  });
  render(
    <MemoryRouter>
      <HistoryTab />
    </MemoryRouter>,
  );
  await screen.findByText("Example");
  fireEvent.click(screen.getByRole("button", { name: "Clear browsing data…" }));
  expect(call.mock.calls.some(([args]) => args.action === "clear-history")).toBe(false);
  expect(screen.getByRole("dialog")).toHaveTextContent("Removes 1 page");
  fireEvent.click(screen.getByRole("radio", { name: "Last hour" }));
  expect(screen.getByRole("button", { name: "Clear 0 pages" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "Cancel" })).toBeEnabled();
  fireEvent.click(screen.getByRole("radio", { name: "Last 7 days" }));
  fireEvent.click(screen.getByRole("button", { name: "Clear 1 page" }));
  await waitFor(() =>
    expect(call).toHaveBeenCalledWith(
      expect.objectContaining({ action: "clear-history", since: expect.any(Number) }),
    ),
  );
  await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
});
it("refreshes a stale retention count and waits for another confirmation", async () => {
  const row = { url: "https://example.test/", title: "Example", visitedAt: Date.now() };
  let saves = 0;
  const call = vi.fn(async (args: Record<string, unknown>) => {
    if (args.action === "list") return { ok: true, history: [row], bookmarks: [] };
    if (args.preview) return { ok: true, removes: 2 };
    if (args.expectedRemoves !== undefined) {
      saves++;
      return saves === 1 ? { ok: false, stale: true, removes: 3 } : { ok: true, removed: 3 };
    }
    return { ok: true, days: 90, entries: 5000 };
  });
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
  const retention = screen.getByRole("combobox", { name: "Keep pages for" });
  await waitFor(() => expect(retention).toBeEnabled());
  fireEvent.change(retention, { target: { value: "1 week" } });
  fireEvent.click(await screen.findByRole("option", { name: "1 week" }));
  await screen.findByRole("button", { name: "Remove 2 pages" });
  expect(saves).toBe(0);
  fireEvent.click(screen.getByRole("button", { name: "Remove 2 pages" }));
  await screen.findByRole("button", { name: "Remove 3 pages" });
  expect(saves).toBe(1);
  fireEvent.click(screen.getByRole("button", { name: "Remove 3 pages" }));
  await waitFor(() => expect(saves).toBe(2));
  await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  expect(call).toHaveBeenCalledWith(
    expect.objectContaining({
      action: "retention",
      days: 7,
      entries: 5000,
      expectedRemoves: 3,
    }),
  );
});

it("reports a failed preview beside the retention controls without changing limits", async () => {
  const call = vi.fn(async (args: Record<string, unknown>) => {
    if (args.action === "list") return { ok: true, history: [], bookmarks: [] };
    if (args.preview) throw new Error("IPC closed");
    return { ok: true, days: 90, entries: 5000 };
  });
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
  const retention = screen.getByRole("combobox", { name: "Keep pages for" });
  await waitFor(() => expect(retention).toBeEnabled());
  fireEvent.change(retention, { target: { value: "1 week" } });
  fireEvent.click(await screen.findByRole("option", { name: "1 week" }));
  expect(await screen.findByText("Could not preview history settings.")).toBeInTheDocument();
  expect(screen.getByRole("combobox", { name: "Keep pages for" })).toHaveValue("3 months");
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
});

it("keeps the day badge consistent with whole-day removal while searching", async () => {
  let rows = [
    { url: "https://example.test/a", title: "Alpha", visitedAt: Date.now() },
    { url: "https://example.test/b", title: "Beta", visitedAt: Date.now() },
  ];
  const all = [...rows];
  const call = vi.fn(async (args: Record<string, unknown>) => {
    if (args.action === "remove-history") {
      rows = [];
      return { ok: true, removed: all };
    }
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
  await screen.findByText("Alpha");
  fireEvent.change(screen.getByRole("textbox", { name: "Search history" }), {
    target: { value: "Alpha" },
  });
  expect(screen.getByText("1 of 2 pages")).toBeInTheDocument();
  expect(screen.getByText("2", { exact: true })).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Remove day" }));
  await waitFor(() =>
    expect(call).toHaveBeenCalledWith({
      action: "remove-history",
      urls: all.map((row) => row.url),
    }),
  );
});
