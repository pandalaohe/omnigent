import { type KeyboardEvent, type UIEvent, useEffect, useMemo, useRef, useState } from "react";
import { Loader2Icon, SearchIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { TablerSvg, tablerValue, useTablerIcons } from "@/lib/tablerIcon";
import { type TablerIconTags, loadTablerIconTags } from "@/lib/tablerIconData";
import { cn } from "@/lib/utils";

const COLUMNS = 8;
const PAGE = 320;

// Useful project choices lead the unfiltered grid; every installed icon follows.
// prettier-ignore
export const TABLER_SUGGESTED = [
  "folder", "briefcase", "rocket", "code", "terminal-2", "database", "server", "cloud",
  "chart-bar", "home", "book", "notes", "bulb", "brand-github", "world", "heart",
  "star", "flame", "bolt", "settings", "tools", "brush", "palette", "camera",
  "music", "device-laptop", "device-mobile", "users", "user", "school", "building", "shopping-cart",
  "coin", "currency-dollar", "calendar", "clock", "mail", "message", "clipboard-list", "checklist",
  "target", "flag", "map-pin", "plane", "car", "leaf", "tree", "bug",
  "robot", "brain", "flask", "puzzle", "package", "lock", "key", "shield",
  "trophy", "gift", "coffee", "pizza",
];

/** Exact and prefix name matches lead, then other name and metadata matches. */
export function searchTablerIcons(
  names: readonly string[],
  tags: TablerIconTags | null,
  query: string,
): string[] {
  const words = query.toLowerCase().trim().split(/\s+/).filter(Boolean);
  if (words.length === 0) return [...names];
  const joined = words.join("-");
  const ranked: [rank: number, name: string][] = [];
  for (const name of names) {
    const meta = tags?.[name];
    const tagText = meta ? [meta.category ?? "", ...(meta.tags ?? [])].join(" ").toLowerCase() : "";
    if (!words.every((word) => name.includes(word) || tagText.includes(word))) continue;
    const rank =
      name === joined
        ? 0
        : name.startsWith(words[0])
          ? 1
          : words.every((word) => name.includes(word))
            ? 2
            : 3;
    ranked.push([rank, name]);
  }
  ranked.sort((a, b) => a[0] - b[0] || a[1].localeCompare(b[1]));
  return ranked.map(([, name]) => name);
}

export function TablerIconPicker({ onSelect }: { onSelect: (value: string) => void }) {
  const [attempt, setAttempt] = useState(0);
  return (
    <div data-testid="tabler-picker" className="flex h-full min-h-0 w-full flex-col bg-popover">
      <PickerBody key={attempt} onSelect={onSelect} onRetry={() => setAttempt((n) => n + 1)} />
    </div>
  );
}

function PickerBody({
  onSelect,
  onRetry,
}: {
  onSelect: (value: string) => void;
  onRetry: () => void;
}) {
  const set = useTablerIcons();
  const [tags, setTags] = useState<TablerIconTags | null>(null);
  const [query, setQuery] = useState("");
  const [limit, setLimit] = useState(PAGE);
  const [active, setActive] = useState(0);
  const [label, setLabel] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const gridRef = useRef<HTMLDivElement>(null);
  const focusPending = useRef(false);

  useEffect(() => {
    let live = true;
    // Tags only widen the search; names still match while they load or if they fail.
    loadTablerIconTags().then(
      (loaded) => live && setTags(loaded),
      () => undefined,
    );
    return () => {
      live = false;
    };
  }, []);

  const names = useMemo(() => {
    if (!set || set === "error") return [];
    const suggested = TABLER_SUGGESTED.filter((name) => name in set);
    const first = new Set(suggested);
    return [...suggested, ...Object.keys(set).filter((name) => !first.has(name))];
  }, [set]);
  const results = useMemo(() => searchTablerIcons(names, tags, query), [names, tags, query]);
  const shown = results.slice(0, limit);

  useEffect(() => {
    if (!focusPending.current) return;
    focusPending.current = false;
    gridRef.current?.querySelector<HTMLButtonElement>(`[data-index="${active}"]`)?.focus();
  }, [active, limit]);

  const focusCell = (index: number) => {
    const next = Math.max(0, Math.min(index, results.length - 1));
    setActive(next);
    const cell = gridRef.current?.querySelector<HTMLButtonElement>(`[data-index="${next}"]`);
    if (cell) {
      cell.focus();
      return;
    }
    focusPending.current = true;
    setLimit((n) => n + PAGE);
  };

  const pick = (name: string) => onSelect(tablerValue(name));

  const onSearchKey = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === "ArrowDown" && results.length > 0) {
      event.preventDefault();
      focusCell(0);
    } else if (event.key === "Enter" && results.length > 0) {
      event.preventDefault();
      pick(results[0]);
    }
  };

  const onCellKey = (event: KeyboardEvent<HTMLButtonElement>, index: number) => {
    const moves: Record<string, number> = {
      ArrowRight: index + 1,
      ArrowLeft: index - 1,
      ArrowDown: index + COLUMNS,
      ArrowUp: index - COLUMNS,
      Home: index - (index % COLUMNS),
      End: index - (index % COLUMNS) + COLUMNS - 1,
    };
    if (!(event.key in moves)) return;
    event.preventDefault();
    if (event.key === "ArrowUp" && index < COLUMNS) {
      inputRef.current?.focus();
      return;
    }
    focusCell(moves[event.key]);
  };

  const onGridScroll = (event: UIEvent<HTMLDivElement>) => {
    const element = event.currentTarget;
    if (
      limit < results.length &&
      element.scrollTop + element.clientHeight > element.scrollHeight - 200
    ) {
      setLimit((n) => n + PAGE);
    }
  };

  return (
    <>
      <div className="flex shrink-0 items-center gap-2 border-b px-3 py-2">
        <SearchIcon aria-hidden="true" className="size-4 shrink-0 text-muted-foreground" />
        <input
          ref={inputRef}
          type="search"
          aria-label="Search icons"
          data-testid="tabler-search"
          placeholder="Search icons"
          autoComplete="off"
          className="w-full min-w-0 bg-transparent text-ui outline-none placeholder:text-muted-foreground"
          value={query}
          onChange={(event) => {
            setQuery(event.target.value);
            setLimit(PAGE);
            setActive(0);
            gridRef.current?.scrollTo?.({ top: 0 });
          }}
          onKeyDown={onSearchKey}
        />
      </div>
      {set === "error" ? (
        <div className="flex flex-1 flex-col items-center justify-center gap-2 p-4 text-center text-ui text-muted-foreground">
          <p role="alert">Icons are offline. Emoji work here.</p>
          <Button type="button" variant="outline" size="sm" onClick={onRetry}>
            Retry icons
          </Button>
        </div>
      ) : set === null ? (
        <div
          className="flex flex-1 items-center justify-center gap-2 text-ui text-muted-foreground"
          data-testid="tabler-loading"
        >
          <Loader2Icon aria-hidden="true" className="size-4 animate-spin" />
          Loading icons
        </div>
      ) : (
        <div
          ref={gridRef}
          role="group"
          aria-label="Tabler icons"
          className="grid min-h-0 flex-1 auto-rows-min grid-cols-8 gap-0.5 overflow-y-auto overscroll-contain p-2"
          onScroll={onGridScroll}
        >
          {shown.length === 0 ? (
            <p className="col-span-8 py-6 text-center text-ui text-muted-foreground">
              No icons match &ldquo;{query.trim()}&rdquo;
            </p>
          ) : (
            shown.map((name, index) => (
              <button
                key={name}
                type="button"
                data-index={index}
                data-testid={`tabler-icon-${name}`}
                tabIndex={index === active ? 0 : -1}
                aria-label={name}
                title={name}
                className={cn(
                  "flex aspect-square items-center justify-center rounded-md text-foreground outline-none transition-colors",
                  "hover:bg-muted focus-visible:bg-muted focus-visible:ring-2 focus-visible:ring-ring/50",
                )}
                onClick={() => pick(name)}
                onKeyDown={(event) => onCellKey(event, index)}
                onFocus={() => {
                  setActive(index);
                  setLabel(name);
                }}
                onMouseEnter={() => setLabel(name)}
                onMouseLeave={() => setLabel(null)}
                onBlur={() => setLabel(null)}
              >
                <TablerSvg node={set[name]} className="size-5" />
              </button>
            ))
          )}
        </div>
      )}
      <div className="flex shrink-0 items-center justify-between gap-2 border-t px-3 py-1.5 text-sm text-muted-foreground">
        <span className="truncate">
          {label ?? (set && set !== "error" ? `${results.length} icons` : "")}
        </span>
        <span className="shrink-0">Tabler Icons</span>
      </div>
    </>
  );
}
