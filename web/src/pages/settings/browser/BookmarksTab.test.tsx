import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import userEvent from "@testing-library/user-event";
import { BookmarksTab } from "./BookmarksTab";
import { expectSettingsPages } from "@/test/settingsPaging";

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
it("pages bookmarks and searches the complete saved list", async () => {
  const bookmarks = Array.from({ length: 45 }, (_, index) => ({
    url: `https://example.test/bookmarks/${index + 1}`,
    title: `Bookmark ${index + 1}`,
  }));
  Object.assign(window, {
    omnigentDesktop: {
      browserPlaces: vi.fn().mockResolvedValue({ ok: true, bookmarks }),
      onBrowserPlaceChosen: () => () => {},
    },
  });
  render(
    <MemoryRouter>
      <BookmarksTab />
    </MemoryRouter>,
  );
  const returnToFirstPage = await expectSettingsPages(
    "Bookmark pages",
    (number) => `Bookmark ${number}`,
  );
  returnToFirstPage();
  fireEvent.change(screen.getByRole("textbox", { name: "Search bookmarks" }), {
    target: { value: "Bookmark 44" },
  });
  expect(screen.getByText("Bookmark 44")).toBeInTheDocument();
  expect(screen.queryByText("Bookmark 45")).not.toBeInTheDocument();
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
it("keeps a page-three bookmark draft saveable through a matching search and paging", async () => {
  let bookmarks = Array.from({ length: 45 }, (_, index) => ({
    url: `https://example.test/bookmarks/${index + 1}`,
    title: `Bookmark ${index + 1}`,
  }));
  const call = vi.fn(
    async ({ action, url, title }: { action: string; url?: string; title?: string }) => {
      if (action === "update") {
        bookmarks = bookmarks.map((row) => (row.url === url ? { ...row, title: title! } : row));
        return { ok: true };
      }
      return { ok: true, bookmarks };
    },
  );
  Object.assign(window, {
    omnigentDesktop: { browserPlaces: call, onBrowserPlaceChosen: () => () => {} },
  });
  render(
    <MemoryRouter>
      <BookmarksTab />
    </MemoryRouter>,
  );
  await expectSettingsPages("Bookmark pages", (number) => `Bookmark ${number}`);
  const row = screen
    .getByRole("button", { name: /Bookmark 44/ })
    .closest<HTMLDivElement>(".group")!;
  fireEvent.click(within(row).getByRole("button", { name: "Edit bookmark" }));
  fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Updated bookmark" } });
  fireEvent.change(screen.getByRole("textbox", { name: "Search bookmarks" }), {
    target: { value: "example.test" },
  });

  expect(screen.getByText("Page 1")).toBeInTheDocument();
  expect(screen.getAllByText(/^Bookmark \d+$/)).toHaveLength(20);
  expect(screen.getByRole("button", { name: "Cancel" })).toBeVisible();
  expect(screen.getByLabelText("Name")).toHaveValue("Updated bookmark");
  fireEvent.click(screen.getByRole("button", { name: "Next" }));
  expect(screen.getByText("Page 2")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Save bookmark" }));
  await waitFor(() =>
    expect(screen.queryByRole("button", { name: "Save bookmark" })).not.toBeInTheDocument(),
  );
  expect(call).toHaveBeenCalledWith({
    action: "update",
    url: "https://example.test/bookmarks/44",
    title: "Updated bookmark",
  });
  expect(screen.getAllByText(/^Bookmark \d+$/)).toHaveLength(20);
  fireEvent.click(screen.getByRole("button", { name: "Next" }));
  expect(screen.getByText("Updated bookmark")).toBeInTheDocument();
});

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
