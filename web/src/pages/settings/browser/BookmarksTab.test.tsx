import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import userEvent from "@testing-library/user-event";
import { BookmarksTab } from "./BookmarksTab";

const notices = vi.hoisted(() => ({
  undo: undefined as undefined | (() => void),
  messages: [] as string[],
}));
vi.mock("sonner", () => ({
  toast: Object.assign(
    (message: string, options?: { action?: { onClick: () => void } }) => {
      notices.messages.push(message);
      if (options?.action) notices.undo = options.action.onClick;
    },
    { error: vi.fn() },
  ),
}));
afterEach(() => {
  cleanup();
  Object.assign(window, { omnigentDesktop: undefined });
  notices.undo = undefined;
  notices.messages = [];
});
it.each([true, false])(
  "deletes immediately and undoes with explicit old-shell position fallback (%s)",
  async (modern) => {
    let rows = [
      { url: "https://example.test/a", title: "Alpha" },
      { url: "https://example.test/b", title: "Beta" },
    ];
    const call = vi.fn(async (args: Record<string, unknown>) => {
      if (args.action === "update")
        rows = args.remove
          ? rows.filter((r) => r.url !== args.url)
          : [...rows, { url: String(args.url), title: String(args.title) }];
      return { ok: true, bookmarks: rows, history: [] };
    });
    Object.assign(window, {
      omnigentDesktop: {
        browserPlaces: call,
        onBrowserPlaceChosen: () => () => {},
        browserPlacesFeatures: modern ? ["update-in-place"] : [],
      },
    });
    render(
      <MemoryRouter>
        <BookmarksTab />
      </MemoryRouter>,
    );
    await screen.findByText("Alpha");
    fireEvent.click(screen.getAllByRole("button", { name: "Delete bookmark" })[1]);
    await waitFor(() => expect(screen.queryByText("Beta")).not.toBeInTheDocument());
    expect(call).toHaveBeenCalledWith({
      action: "update",
      url: "https://example.test/b",
      remove: true,
    });
    notices.undo?.();
    await screen.findByText("Beta");
    expect(call).toHaveBeenCalledWith(
      expect.objectContaining(modern ? { index: 1 } : { title: "Beta" }),
    );
    if (!modern)
      await waitFor(() =>
        expect(notices.messages).toContain(
          "Restored at the top. Update the desktop app to keep its place.",
        ),
      );
  },
);
it("validates URL and duplicates and preserves Name/URL edits until Save", async () => {
  const call = vi.fn(async () => ({
    ok: true,
    bookmarks: [{ url: "https://example.test/", title: "Example" }],
    history: [],
  }));
  Object.assign(window, {
    omnigentDesktop: { browserPlaces: call, onBrowserPlaceChosen: () => () => {} },
  });
  render(
    <MemoryRouter>
      <BookmarksTab />
    </MemoryRouter>,
  );
  await screen.findByText("Example");
  fireEvent.click(screen.getByRole("button", { name: "Add bookmark" }));
  fireEvent.change(screen.getByLabelText("URL"), { target: { value: "file:///tmp/page" } });
  expect(screen.getByRole("button", { name: "Save bookmark" })).toBeDisabled();
  fireEvent.change(screen.getByLabelText("URL"), { target: { value: "https://example.test/" } });
  expect(screen.getByRole("alert")).toHaveTextContent("Already bookmarked");
  fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
  expect(call).toHaveBeenCalledTimes(1);
});

it("adds a bookmark through Save and closes the editor after refreshing the list", async () => {
  const user = userEvent.setup();
  let rows = [{ url: "https://example.test/", title: "Example" }];
  const call = vi.fn(async (args: Record<string, unknown>) => {
    if (args.action === "update") {
      rows = [{ url: String(args.url), title: String(args.title) }, ...rows];
      return { ok: true };
    }
    return { ok: true, bookmarks: [...rows], history: [] };
  });
  Object.assign(window, {
    omnigentDesktop: { browserPlaces: call, onBrowserPlaceChosen: () => () => {} },
  });
  render(
    <MemoryRouter>
      <BookmarksTab />
    </MemoryRouter>,
  );
  await screen.findByText("Example");
  await user.click(screen.getByRole("button", { name: "Add bookmark" }));
  await user.type(screen.getByLabelText("Name"), "New example");
  await user.type(screen.getByLabelText("URL"), "https://new.example.test/");
  await user.click(screen.getByRole("button", { name: "Save bookmark" }));
  expect(await screen.findByText("New example", { exact: true })).toBeVisible();
  expect(screen.queryByLabelText("Name")).not.toBeInTheDocument();
  expect(screen.queryByLabelText("URL")).not.toBeInTheDocument();
});
