import { useEffect, useState } from "react";
import { toast } from "sonner";
import { CopyIcon, StarIcon, TrashIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Combobox, COMBOBOX_OPTION_THRESHOLD } from "@/components/ui/combobox";
import {
  Select,
  SelectTrigger,
  SelectValue,
  SelectContent,
  SelectItem,
} from "@/components/ui/select";
import { Segmented } from "@/components/ui/segmented";
import type { BrowserPlace } from "@/lib/browserPlacesBridge";
import {
  BrowserGroup,
  UpdateReason,
  SearchPages,
  PageTile,
  Highlight,
  RowActions,
  Confirm,
  usePlaces,
  useOpenPage,
  undoToast,
  copyPage,
  pageCount,
} from "./browserSettingsParts";

export function HistoryTab() {
  const places = usePlaces(),
    open = useOpenPage();
  const [retentionMessage, setRetentionMessage] = useState(""),
    [previewing, setPreviewing] = useState(false);
  const [query, setQuery] = useState(""),
    [visible, setVisible] = useState(50),
    [clear, setClear] = useState(false),
    [range, setRange] = useState("all"),
    [limits, setLimits] = useState({ days: 90, entries: 5000 }),
    [pending, setPending] = useState<{ days: number; entries: number; removes: number } | null>(
      null,
    );
  const removal = places.has("remove-history"),
    retention = places.has("retention"),
    sinceCap = places.has("clear-since");
  useEffect(() => {
    if (retention)
      void places.bridge?.browserPlaces({ action: "retention" }).then((result) => {
        if (result.ok) setLimits({ days: result.days ?? 90, entries: result.entries ?? 5000 });
      });
  }, [retention, places.bridge]);
  const filtered = places.history.filter((row) =>
    `${row.title} ${row.url}`.toLowerCase().includes(query.toLowerCase()),
  );
  const groups = new Map<string, BrowserPlace[]>();
  for (const row of filtered.slice(0, visible)) {
    const date = new Date(row.visitedAt ?? 0).toDateString();
    groups.set(date, [...(groups.get(date) ?? []), row]);
  }
  const now = new Date(),
    today = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime(),
    yesterday = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1).getTime();
  const since =
    range === "hour"
      ? Date.now() - 3600000
      : range === "today"
        ? today
        : range === "week"
          ? Date.now() - 7 * 86400000
          : undefined;
  const clears = places.history.filter(
    (row) => since === undefined || (row.visitedAt ?? 0) >= since,
  ).length;
  async function remove(rows: BrowserPlace[]) {
    const result = await places.run({ action: "remove-history", urls: rows.map((row) => row.url) });
    if (result.ok && Array.isArray(result.removed))
      undoToast(`Removed ${pageCount(result.removed.length)}.`, async () => {
        if (!(await places.run({ action: "restore-history", rows: result.removed })).ok)
          throw Error();
      });
  }
  async function toggleBookmark(row: BrowserPlace) {
    const saved = places.bookmarks.find((value) => value.url === row.url);
    if (!saved) {
      await places.run({ action: "update", url: row.url, title: row.title });
      return;
    }
    const index = places.bookmarks.indexOf(saved),
      inplace = places.has("update-in-place");
    if ((await places.run({ action: "update", url: row.url, remove: true })).ok)
      undoToast("Bookmark removed.", async () => {
        const result = await places.run({
          action: "update",
          url: saved.url,
          title: saved.title,
          ...(inplace ? { index } : {}),
        });
        if (!result.ok) throw Error();
        if (!inplace) toast("Restored at the top. Update the desktop app to keep its place.");
      });
  }
  async function saveLimits(next: { days: number; entries: number }, expectedRemoves: number) {
    const { days, entries } = next;
    const result = await places.run({ action: "retention", days, entries, expectedRemoves });
    if (result.stale) {
      setPending({ days, entries, removes: result.removes ?? 0 });
      setRetentionMessage("History changed. Review the new count and confirm again.");
    } else if (result.ok) {
      setLimits({ days, entries });
      setPending(null);
      setRetentionMessage("Saved");
    } else setRetentionMessage("Could not save history settings.");
  }
  async function preview(next: { days: number; entries: number }) {
    setPreviewing(true);
    setRetentionMessage("");
    try {
      const result = await places.bridge?.browserPlaces({
        action: "retention",
        ...next,
        preview: true,
      });
      if (!result?.ok) {
        setRetentionMessage("Could not preview history settings.");
        return;
      }
      if (result.removes) setPending({ ...next, removes: result.removes });
      else await saveLimits(next, 0);
    } catch {
      setRetentionMessage("Could not preview history settings.");
    } finally {
      setPreviewing(false);
    }
  }
  return (
    <>
      <BrowserGroup title="History" wide>
        <div className="mb-4 flex flex-wrap gap-3">
          <SearchPages
            kind="history"
            query={query}
            setQuery={(value) => {
              setQuery(value);
              setVisible(50);
            }}
            total={places.history.length}
            count={filtered.length}
          />
          <Button
            variant="outline"
            disabled={places.busy || !places.history.length}
            onClick={() => setClear(true)}
          >
            Clear browsing data…
          </Button>
        </div>
        {!removal && <UpdateReason verb="remove single pages" />}
        <p className="text-xs text-muted-foreground">
          Showing {Math.min(visible, filtered.length)} of {places.history.length}
        </p>
        <div className="max-h-96 overflow-auto">
          {[...groups].map(([day, rows]) => {
            const date = new Date(day),
              label =
                date.getTime() === today
                  ? "Today"
                  : date.getTime() === yesterday
                    ? "Yesterday"
                    : date.toLocaleDateString(undefined, {
                        weekday: "long",
                        month: "long",
                        day: "numeric",
                      });
            return (
              <div key={day}>
                <div className="flex items-center gap-2 border-t py-3">
                  <h3 className="text-sm font-medium">
                    <span className="md:hidden">{label}</span>
                    <span className="hidden md:inline">
                      {label === "Today" || label === "Yesterday"
                        ? `${label} – ${date.toLocaleDateString(undefined, { weekday: "long", month: "long", day: "numeric" })}`
                        : label}
                    </span>
                  </h3>
                  <span className="rounded bg-muted px-2 text-xs text-muted-foreground">
                    {
                      places.history.filter(
                        (row) => new Date(row.visitedAt ?? 0).toDateString() === day,
                      ).length
                    }
                  </span>
                  {removal && (
                    <Button
                      variant="ghost"
                      size="sm"
                      className="ml-auto"
                      disabled={places.busy}
                      onClick={() =>
                        void remove(
                          places.history.filter(
                            (row) => new Date(row.visitedAt ?? 0).toDateString() === day,
                          ),
                        )
                      }
                    >
                      Remove day
                    </Button>
                  )}
                </div>
                {rows.map((row) => (
                  <div key={row.url} className="group flex min-w-0 items-center gap-3 py-2">
                    <PageTile url={row.url} />
                    <button
                      type="button"
                      onClick={() => open(row.url)}
                      className="min-w-0 flex-1 text-left"
                    >
                      <p className="truncate text-sm">
                        <Highlight text={row.title} query={query} />
                      </p>
                      <p className="truncate text-xs text-muted-foreground">
                        <Highlight text={new URL(row.url).host} query={query} />
                      </p>
                    </button>
                    <span className="hidden text-xs text-muted-foreground md:block">
                      {new Date(row.visitedAt ?? 0).toLocaleTimeString(undefined, {
                        hour: "2-digit",
                        minute: "2-digit",
                      })}
                    </span>
                    <RowActions
                      actions={[
                        {
                          label: places.bookmarks.some((saved) => saved.url === row.url)
                            ? "Remove bookmark"
                            : "Bookmark page",
                          icon: (
                            <StarIcon
                              className={`size-4 ${places.bookmarks.some((saved) => saved.url === row.url) ? "fill-current" : ""}`}
                            />
                          ),
                          disabled: places.busy,
                          run: () => void toggleBookmark(row),
                        },
                        {
                          label: "Copy link",
                          icon: <CopyIcon className="size-4" />,
                          run: () => void copyPage(row.url),
                        },
                        ...(removal
                          ? [
                              {
                                label: "Remove page",
                                icon: <TrashIcon className="size-4" />,
                                disabled: places.busy,
                                run: () => void remove([row]),
                              },
                            ]
                          : []),
                      ]}
                    />
                  </div>
                ))}
              </div>
            );
          })}
        </div>
        {!filtered.length && (
          <p className="py-6 text-sm text-muted-foreground">
            {query ? `No pages match “${query}”` : "Your recently visited pages will appear here."}
            {query && (
              <Button variant="ghost" onClick={() => setQuery("")}>
                Clear search
              </Button>
            )}
          </p>
        )}
        {filtered.length > visible && (
          <Button variant="outline" onClick={() => setVisible(visible + 50)}>
            Show {Math.min(50, filtered.length - visible)} more
          </Button>
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
      <BrowserGroup title="Keep history">
        {(["days", "entries"] as const).map((key) => {
          const options = key === "days" ? [0, 7, 30, 90, 365, 3650] : [1000, 5000, 20000, 100000];
          const names =
            key === "days"
              ? ["Don't keep", "1 week", "1 month", "3 months", "1 year", "10 years"]
              : options.map((value) => `${value.toLocaleString()} pages`);
          return (
            <div key={key} className="mb-4 flex flex-wrap items-center justify-between gap-3">
              <span className="text-sm">{key === "days" ? "Keep pages for" : "Keep at most"}</span>
              {options.length > COMBOBOX_OPTION_THRESHOLD ? (
                <fieldset disabled={!retention || places.busy || previewing} className="contents">
                  <Combobox
                    key={`${key}:${limits[key]}:${previewing}`}
                    mode="select"
                    options={[
                      ...(!options.includes(limits[key])
                        ? [
                            {
                              key: String(limits[key]),
                              label: `Custom (${limits[key]} ${key === "days" ? "days" : "pages"})`,
                            },
                          ]
                        : []),
                      ...options.map((value, index) => ({
                        key: String(value),
                        label: names[index],
                      })),
                    ]}
                    value={String(limits[key])}
                    onValueChange={(value) => void preview({ ...limits, [key]: Number(value) })}
                    aria-label={key === "days" ? "Keep pages for" : "Keep at most"}
                    className="h-auto w-40 rounded-none! bg-transparent p-0"
                  />
                </fieldset>
              ) : (
                <Select
                  value={String(limits[key])}
                  disabled={!retention || places.busy || previewing}
                  onValueChange={(value) => void preview({ ...limits, [key]: Number(value) })}
                >
                  <SelectTrigger
                    aria-label={key === "days" ? "Keep pages for" : "Keep at most"}
                    className="w-40"
                  >
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {!options.includes(limits[key]) && (
                      <SelectItem value={String(limits[key])}>
                        Custom ({limits[key]} {key === "days" ? "days" : "pages"})
                      </SelectItem>
                    )}
                    {options.map((value, index) => (
                      <SelectItem key={value} value={String(value)}>
                        {names[index]}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              )}
            </div>
          );
        })}
        {!retention && <UpdateReason verb="change history limits" />}
        <p role="status" className="text-sm">
          {retentionMessage}
        </p>
      </BrowserGroup>
      <Confirm
        title="Clear browsing data"
        description={`Removes ${pageCount(clears)} from history. Bookmarks, sign-ins and extensions are kept.`}
        action={`Clear ${pageCount(clears)}`}
        open={clear}
        busy={places.busy}
        disabled={clears === 0}
        onClose={() => setClear(false)}
        onConfirm={() => {
          void places
            .run({ action: "clear-history", ...(since === undefined ? {} : { since }) })
            .then((result) => {
              if (result.ok) setClear(false);
            });
        }}
      >
        <Segmented
          label="Time range"
          value={range}
          onChange={setRange}
          options={(sinceCap
            ? [
                ["hour", "Last hour"],
                ["today", "Today"],
                ["week", "Last 7 days"],
                ["all", "All time"],
              ]
            : [["all", "All time"]]
          ).map(([value, label]) => ({ value, label }))}
        />
        {!sinceCap && <UpdateReason verb="clear a time range" />}
      </Confirm>
      <Confirm
        title="Shorten history retention?"
        description={`Removes ${pageCount(pending?.removes ?? 0)} permanently. Bookmarks are kept.`}
        action={`Remove ${pageCount(pending?.removes ?? 0)}`}
        open={!!pending}
        busy={places.busy}
        onClose={() => setPending(null)}
        onConfirm={() => {
          if (pending) void saveLimits(pending, pending.removes);
        }}
      />
    </>
  );
}
