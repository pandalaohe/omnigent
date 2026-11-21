import { useMemo, useState } from "react";
import { CopyIcon, PencilIcon, TrashIcon } from "lucide-react";
import { toast } from "sonner";
import { SettingsPager } from "@/components/SettingsPager";
import { usePagedRows } from "@/hooks/usePagedRows";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import type { BrowserPlace } from "@/lib/browserPlacesBridge";
import {
  BrowserGroup,
  UpdateReason,
  SearchPages,
  PageTile,
  Highlight,
  RowActions,
  usePlaces,
  useOpenPage,
  undoToast,
  copyPage,
} from "./browserSettingsParts";

export function BookmarksTab() {
  const places = usePlaces(),
    open = useOpenPage();
  const [query, setQuery] = useState(""),
    [edit, setEdit] = useState<{ row?: BrowserPlace; title: string; url: string } | null>(null);
  const inplace = places.has("update-in-place");
  const { bookmarks } = places;
  const filter = query.toLowerCase();
  const filtered = useMemo(
    () => bookmarks.filter((row) => `${row.title} ${row.url}`.toLowerCase().includes(filter)),
    [bookmarks, filter],
  );
  const { pageRows, ...pager } = usePagedRows(filtered, { resetKey: filter });
  let error = "";
  let url = "";
  if (edit) {
    try {
      const parsed = new URL(edit.url);
      if (!["http:", "https:"].includes(parsed.protocol) || parsed.username || parsed.password)
        throw Error();
      url = parsed.href;
    } catch {
      error = "Enter a web address that starts with http:// or https://";
    }
    const duplicate = places.bookmarks.find((row) => row.url === url && row.url !== edit.row?.url);
    if (duplicate) error = `Already bookmarked as “${duplicate.title}”.`;
  }
  async function save() {
    if (!edit || error) return;
    const result = await places.run({
      action: "update",
      url,
      title: edit.title,
      ...(inplace && edit.row ? { previousUrl: edit.row.url } : {}),
    });
    if (result.ok) setEdit(null);
  }
  async function remove(row: BrowserPlace) {
    const index = places.bookmarks.indexOf(row);
    if (!(await places.run({ action: "update", url: row.url, remove: true })).ok) return;
    undoToast("Bookmark removed.", async () => {
      const result = await places.run({
        action: "update",
        url: row.url,
        title: row.title,
        ...(inplace ? { index } : {}),
      });
      if (!result.ok) throw Error();
      if (!inplace) toast("Restored at the top. Update the desktop app to keep its place.");
    });
  }
  function editor() {
    return (
      edit && (
        <form
          className="space-y-3 rounded-lg border p-3"
          onSubmit={(e) => {
            e.preventDefault();
            void save();
          }}
          onKeyDown={(e) => {
            if (e.key === "Escape") setEdit(null);
          }}
        >
          <label className="block text-sm">
            Name
            <Input
              autoFocus
              value={edit.title}
              onChange={(e) => setEdit({ ...edit, title: e.target.value })}
            />
          </label>
          <label className="block text-sm">
            URL
            <Input
              value={edit.url}
              disabled={!!edit.row && !inplace}
              onChange={(e) => setEdit({ ...edit, url: e.target.value })}
            />
          </label>
          {edit.row && !inplace && <UpdateReason verb="edit bookmark addresses" />}
          {error && (
            <p role="alert" className="text-sm text-destructive">
              {error}
            </p>
          )}
          <div className="flex justify-end gap-2">
            <Button type="button" variant="outline" onClick={() => setEdit(null)}>
              Cancel
            </Button>
            <Button variant="outline" disabled={!!error || places.busy}>
              Save bookmark
            </Button>
          </div>
        </form>
      )
    );
  }
  return (
    <BrowserGroup title="Bookmarks" wide>
      <div className="mb-4 flex flex-wrap gap-3">
        <SearchPages
          kind="bookmarks"
          query={query}
          setQuery={setQuery}
          total={places.bookmarks.length}
          count={filtered.length}
        />
        <Button variant="outline" onClick={() => setEdit({ title: "", url: "" })}>
          Add bookmark
        </Button>
      </div>
      {editor()}
      <p className="text-xs text-muted-foreground">
        Showing {pageRows.length} of {places.bookmarks.length}
      </p>
      <div>
        {pageRows.map((row) =>
          edit?.row?.url !== row.url ? (
            <div key={row.url} className="group flex min-w-0 items-center gap-3 border-t py-3">
              <PageTile url={row.url} />
              <button
                type="button"
                className="min-w-0 flex-1 text-left"
                onClick={() => open(row.url)}
              >
                <p className="truncate text-sm">
                  <Highlight text={row.title} query={query} />
                </p>
                <p className="truncate text-xs text-muted-foreground">
                  <Highlight
                    text={new URL(row.url).host + new URL(row.url).pathname}
                    query={query}
                  />
                </p>
              </button>
              <RowActions
                actions={[
                  {
                    label: "Edit bookmark",
                    icon: <PencilIcon className="size-4" />,
                    run: () => setEdit({ row, title: row.title, url: row.url }),
                  },
                  {
                    label: "Copy link",
                    icon: <CopyIcon className="size-4" />,
                    run: () => void copyPage(row.url),
                  },
                  {
                    label: "Delete bookmark",
                    icon: <TrashIcon className="size-4" />,
                    disabled: places.busy,
                    run: () => void remove(row),
                  },
                ]}
              />
            </div>
          ) : null,
        )}
      </div>
      <SettingsPager label="Bookmark pages" {...pager} />
      {!filtered.length && (
        <p className="py-6 text-sm text-muted-foreground">
          {query ? `No pages match “${query}”` : "No bookmarks yet. Add a page to keep it here."}
          {query && (
            <Button variant="ghost" onClick={() => setQuery("")}>
              Clear search
            </Button>
          )}
        </p>
      )}
      <p role="status" className="text-sm">
        {places.message}
      </p>
      {places.refreshError && (
        <p role="alert" className="text-sm text-destructive">
          {places.refreshError}
        </p>
      )}
    </BrowserGroup>
  );
}
