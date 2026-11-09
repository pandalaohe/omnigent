import { useCallback, useEffect, useMemo, useState } from "react";
import { Button } from "@/components/ui/button";
import { browserPlacesBridge, type BrowserPlace } from "@/lib/browserPlacesBridge";

export function BrowserPlacesSettings() {
  const bridge = useMemo(browserPlacesBridge, []);
  const [bookmarks, setBookmarks] = useState<BrowserPlace[]>([]);
  const [history, setHistory] = useState<BrowserPlace[]>([]);
  const [query, setQuery] = useState("");
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [confirmClear, setConfirmClear] = useState(false);
  const refresh = useCallback(
    () =>
      bridge?.browserPlaces({ action: "list" }).then((result) => {
        if (!result.ok) throw new Error(result.error);
        setBookmarks(result.bookmarks || []);
        setHistory(result.history || []);
      }),
    [bridge],
  );
  useEffect(() => {
    void refresh()?.catch(() => setMessage("Could not read saved pages."));
  }, [refresh]);
  if (!bridge) return null;
  const change = async (args: Record<string, unknown>, success: string) => {
    setBusy(true);
    try {
      const result = await bridge.browserPlaces(args);
      if (!result.ok) throw new Error(result.error);
      await refresh();
      setMessage(success);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Could not save changes.");
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="flex flex-col gap-3">
      <p className="text-muted-foreground text-xs">
        Bookmarks and history stay on this desktop, across connected servers. Manage them here; the
        address bar suggests saved and recently visited pages. Files: browser-bookmarks.json and
        browser-history.json in the app data directory. History keeps up to 5,000 pages for 90 days
        by default; change browser_history_max_entries and browser_history_max_days in
        settings.json.
      </p>
      <label className="flex items-center gap-2 text-sm">
        Find saved pages
        <input
          className="min-w-0 flex-1 rounded border border-input bg-transparent px-2 py-1"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
        />
      </label>
      <h4 className="font-medium text-sm">Bookmarks ({bookmarks.length})</h4>
      <div className="max-h-60 overflow-auto">
        {bookmarks
          .filter((row) => `${row.title} ${row.url}`.toLowerCase().includes(query.toLowerCase()))
          .map((row) => (
            <form
              key={row.url}
              className="flex flex-wrap items-center gap-2 border-border border-b py-2"
              onSubmit={(event) => {
                event.preventDefault();
                const title = new FormData(event.currentTarget).get("title");
                void change({ action: "update", url: row.url, title }, "Bookmark renamed.");
              }}
            >
              <div className="min-w-0 flex-1">
                <input
                  aria-label={`Bookmark name for ${row.url}`}
                  name="title"
                  key={row.title}
                  defaultValue={row.title}
                  className="w-full rounded border border-input bg-transparent px-2 py-1 text-sm"
                />
                <p className="truncate text-muted-foreground text-xs" title={row.url}>
                  {row.url}
                </p>
              </div>
              <Button type="submit" variant="outline" size="sm" disabled={busy}>
                Rename
              </Button>
              <Button
                type="button"
                variant="outline"
                size="sm"
                disabled={busy}
                onClick={() =>
                  void change({ action: "update", url: row.url, remove: true }, "Bookmark removed.")
                }
              >
                Remove
              </Button>
            </form>
          ))}
        {!bookmarks.length && (
          <p className="text-muted-foreground text-xs">
            Use the star beside the address bar to save a page.
          </p>
        )}
      </div>
      <div className="flex items-center gap-2">
        <h4 className="font-medium text-sm">History ({history.length})</h4>
        <Button
          variant="outline"
          size="sm"
          disabled={busy || !history.length}
          onClick={() => setConfirmClear(true)}
        >
          Clear history
        </Button>
      </div>
      {confirmClear && (
        <div className="flex items-center gap-2 text-sm">
          Permanently clear all history on this desktop?
          <Button
            variant="destructive"
            size="sm"
            disabled={busy}
            onClick={() => {
              void change({ action: "clear-history" }, "History cleared.");
              setConfirmClear(false);
            }}
          >
            Clear all history
          </Button>
          <Button variant="outline" size="sm" onClick={() => setConfirmClear(false)}>
            Cancel
          </Button>
        </div>
      )}
      <div className="max-h-60 overflow-auto">
        {history
          .filter((row) => `${row.title} ${row.url}`.toLowerCase().includes(query.toLowerCase()))
          .slice(0, 100)
          .map((row) => (
            <div key={row.url} className="border-border border-b py-1 text-xs">
              <p className="truncate">{row.title}</p>
              <p className="truncate text-muted-foreground" title={row.url}>
                {row.url}
              </p>
            </div>
          ))}
      </div>
      <p role="status" className="text-xs">
        {message}
      </p>
    </div>
  );
}
