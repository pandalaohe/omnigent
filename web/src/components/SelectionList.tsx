import { useEffect, useMemo, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { useVirtualizer } from "@tanstack/react-virtual";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { resolveMulti, suggestMode, summarizeMulti, type MultiValue } from "@/lib/multiSelection";

export interface PickerItem {
  id: string;
  label: string;
  description?: string;
  keywords?: string;
  section?: string;
  unavailableReason?: string | null;
  aside?: ReactNode;
  disabled?: boolean;
}

interface Common {
  items: PickerItem[];
  searchLabel: string;
  searchThreshold?: number;
  listLabel?: string;
  sectionOrder?: string[];
  sectionGlosses?: Record<string, string>;
  unavailableLabel?: string;
  loading?: ReactNode;
  error?: ReactNode;
  emptyLabel?: string;
  onConfirm?: () => void;
  onQueryChange?: (query: string) => void;
  rowRole?: "button" | "option";
}
type Single = Common & {
  mode: "single";
  value: string | null;
  onChange: (id: string | null) => void;
  noneLabel?: string;
};
type Multi = Common & {
  mode: "multi";
  value: MultiValue;
  onChange: (value: MultiValue) => void;
  pinned?: (id: string) => string[];
};
type Props = Single | Multi;
type Entry = { kind: "heading"; label: string; gloss?: string } | { kind: "row"; item: PickerItem };

export function SelectionList(props: Props) {
  const [query, setQuery] = useState("");
  const [expanded, setExpanded] = useState<string[]>([]);
  const [chipsExpanded, setChipsExpanded] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);
  const [active, setActive] = useState(0);
  const [focusId, setFocusId] = useState<string | null>(null);
  const searchRef = useRef<HTMLInputElement>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const [drafts, setDrafts] = useState<{ only: string[]; except: string[] }>(() => ({
    only: props.mode === "multi" && props.value.mode === "only" ? props.value.ids : [],
    except: props.mode === "multi" && props.value.mode === "except" ? props.value.ids : [],
  }));
  const showSearch =
    props.searchThreshold === 0 || props.items.length > (props.searchThreshold ?? 8);
  useEffect(() => {
    if (showSearch) searchRef.current?.focus();
  }, [showSearch]);

  const matches = useMemo(
    () =>
      props.items.filter((item) =>
        `${item.label} ${item.description ?? ""} ${item.section ?? ""} ${item.keywords ?? ""}`
          .toLowerCase()
          .includes(query.trim().toLowerCase()),
      ),
    [props.items, query],
  );
  const applicable = matches.filter((item) => !item.unavailableReason);
  const skipped = matches.filter((item) => !!item.unavailableReason);
  const sections =
    props.sectionOrder ?? Array.from(new Set(applicable.map((item) => item.section ?? "")));
  const entries: Entry[] = [];
  for (const section of sections) {
    const rows = applicable.filter((item) => (item.section ?? "") === section);
    if (!rows.length) continue;
    if (section)
      entries.push({ kind: "heading", label: section, gloss: props.sectionGlosses?.[section] });
    entries.push(...rows.map((item): Entry => ({ kind: "row", item })));
  }
  for (const reason of new Set(skipped.map((item) => item.unavailableReason!))) {
    const rows = skipped.filter((item) => item.unavailableReason === reason);
    entries.push({
      kind: "heading",
      label: `${props.unavailableLabel ?? "Skipped"} (${rows.length}) · ${reason}`,
    });
    if (expanded.includes(reason))
      entries.push(...rows.map((item): Entry => ({ kind: "row", item })));
  }
  const virtual = useVirtualizer({
    count: entries.length,
    getScrollElement: () => scrollRef.current,
    estimateSize: (index) => (entries[index]?.kind === "heading" ? 44 : 48),
    overscan: 6,
    enabled: props.items.length > 200,
  });
  const virtualItems = virtual.getVirtualItems();
  useEffect(() => {
    if (!focusId) return;
    const button = Array.from(
      scrollRef.current?.querySelectorAll<HTMLButtonElement>("[data-picker-item]") ?? [],
    ).find((candidate) => candidate.dataset.pickerItem === focusId);
    if (button) {
      button.focus();
      setFocusId(null);
    }
  }, [focusId, virtualItems]);
  const rowItems = entries.filter(
    (entry): entry is Extract<Entry, { kind: "row" }> =>
      entry.kind === "row" && !entry.item.disabled && !entry.item.unavailableReason,
  );
  const groupedEntries: { heading?: Extract<Entry, { kind: "heading" }>; rows: PickerItem[] }[] =
    [];
  for (const entry of entries) {
    if (entry.kind === "heading") groupedEntries.push({ heading: entry, rows: [] });
    else {
      if (groupedEntries.length === 0) groupedEntries.push({ rows: [] });
      groupedEntries[groupedEntries.length - 1].rows.push(entry.item);
    }
  }
  const choose = (item: PickerItem) => {
    if (item.disabled || item.unavailableReason) return;
    if (props.mode === "single") {
      props.onChange(item.id);
      props.onConfirm?.();
      return;
    }
    const checked = resolveMulti(props.value, [item]).length > 0;
    const ids = new Set(props.value.ids);
    if (props.value.mode === "only") {
      if (checked) ids.delete(item.id);
      else ids.add(item.id);
    } else if (checked) ids.add(item.id);
    else ids.delete(item.id);
    const next = { mode: props.value.mode, ids: [...ids] };
    setDrafts((current) => ({ ...current, [next.mode]: next.ids }));
    props.onChange(next);
  };
  const bulk = (action: "all" | "none" | "invert") => {
    if (props.mode !== "multi") return;
    const ids = new Set(props.value.ids);
    for (const item of applicable.filter((candidate) => !candidate.disabled)) {
      const checked = resolveMulti({ ...props.value, ids: [...ids] }, [item]).length > 0;
      const nextChecked = action === "invert" ? !checked : action === "all";
      if (props.value.mode === "only") {
        if (nextChecked) ids.add(item.id);
        else ids.delete(item.id);
      } else if (nextChecked) ids.delete(item.id);
      else ids.add(item.id);
    }
    const next = { mode: props.value.mode, ids: [...ids] };
    setDrafts((current) => ({ ...current, [next.mode]: next.ids }));
    props.onChange(next);
    setMenuOpen(false);
  };
  const handleKey = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      if (!rowItems.length) return;
      event.preventDefault();
      const focusedId = (event.target as HTMLElement).closest<HTMLElement>("[data-picker-item]")
        ?.dataset.pickerItem;
      const focusedIndex = rowItems.findIndex((row) => row.item.id === focusedId);
      const from =
        focusedIndex >= 0 ? focusedIndex : event.target === searchRef.current ? -1 : active;
      const next = Math.max(
        0,
        Math.min(rowItems.length - 1, from + (event.key === "ArrowDown" ? 1 : -1)),
      );
      setActive(next);
      setFocusId(rowItems[next].item.id);
      if (props.items.length > 200)
        virtual.scrollToIndex(
          entries.findIndex(
            (entry) => entry.kind === "row" && entry.item.id === rowItems[next]?.item.id,
          ),
        );
    } else if (
      event.key === "Enter" &&
      props.mode === "multi" &&
      props.onConfirm &&
      (event.target === searchRef.current ||
        !!(event.target as HTMLElement).closest("[data-picker-item]"))
    ) {
      event.preventDefault();
      props.onConfirm();
    } else if (event.key === " " && (event.target as HTMLElement).closest("[data-picker-item]")) {
      event.preventDefault();
      const item = rowItems.find(
        (row) =>
          row.item.id ===
          (event.target as HTMLElement).closest<HTMLElement>("[data-picker-item]")?.dataset
            .pickerItem,
      )?.item;
      if (item) choose(item);
    }
  };
  const renderEntry = (entry: Entry) => {
    if (entry.kind === "heading") {
      const reason = skipped.find((item) =>
        entry.label.endsWith(item.unavailableReason!),
      )?.unavailableReason;
      return reason ? (
        <button
          type="button"
          aria-label={
            props.unavailableLabel ? `${props.unavailableLabel} (${skipped.length})` : undefined
          }
          className="min-h-11 w-full border-t px-3 text-left text-sm font-medium"
          aria-expanded={expanded.includes(reason)}
          onClick={() =>
            setExpanded((current) =>
              current.includes(reason) ? current.filter((r) => r !== reason) : [...current, reason],
            )
          }
        >
          {entry.label}
        </button>
      ) : (
        <div className="sticky top-0 z-10 bg-popover px-3 py-2 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
          {entry.label}
          {entry.gloss && <p className="font-normal normal-case tracking-normal">{entry.gloss}</p>}
        </div>
      );
    }
    const { item } = entry;
    const checked =
      props.mode === "single"
        ? props.value === item.id
        : resolveMulti(props.value, [item]).length > 0;
    return (
      <button
        key={item.id}
        data-picker-item={item.id}
        type="button"
        role={props.mode === "single" ? (props.rowRole ?? "option") : "checkbox"}
        aria-selected={props.mode === "single" ? checked : undefined}
        aria-checked={props.mode === "multi" ? checked : undefined}
        disabled={item.disabled || !!item.unavailableReason}
        onClick={() => choose(item)}
        onFocus={() => setActive(rowItems.findIndex((row) => row.item.id === item.id))}
        className="flex min-h-11 w-full items-center justify-between gap-3 rounded-lg px-3 py-2 text-left text-sm hover:bg-accent focus-visible:outline-2 focus-visible:outline-offset-[-2px] focus-visible:outline-ring disabled:cursor-not-allowed disabled:opacity-55"
      >
        <span className="flex min-w-0 items-center gap-2">
          {props.mode === "multi" && (
            <input tabIndex={-1} type="checkbox" readOnly checked={checked} aria-hidden="true" />
          )}
          <span className="min-w-0">
            <span className="block truncate font-medium">{item.label}</span>
            {(item.unavailableReason || item.description) && (
              <span className="block text-xs text-muted-foreground">
                {item.unavailableReason ?? item.description}
              </span>
            )}
          </span>
        </span>
        {item.aside && <span className="shrink-0 text-xs text-muted-foreground">{item.aside}</span>}
      </button>
    );
  };
  const multi = props.mode === "multi" ? props : null;
  const summary = multi ? summarizeMulti(multi.value, multi.items) : null;
  const suggestion = multi ? suggestMode(multi.value, multi.items) : null;
  const marked = multi
    ? multi.value.ids.filter((id) =>
        multi.items.some((item) => item.id === id && !item.unavailableReason),
      )
    : [];
  return (
    <div className="flex min-h-0 flex-col" onKeyDown={handleKey}>
      {multi && (
        <>
          <div className="flex gap-1 px-4 pt-3" role="group" aria-label="Selection mode">
            {(["except", "only"] as const).map((mode) => (
              <Button
                key={mode}
                type="button"
                size="sm"
                variant={multi.value.mode === mode ? "secondary" : "ghost"}
                aria-pressed={multi.value.mode === mode}
                onClick={() => multi.onChange({ mode, ids: drafts[mode] })}
              >
                {mode === "except" ? "All, except these" : "Only these"}
              </Button>
            ))}
          </div>
          {marked.length > 0 && (
            <div className="flex flex-wrap items-center gap-1 px-4 pt-2 text-xs">
              <span className="text-muted-foreground">
                {multi.value.mode === "except" ? "Except:" : "Selected:"}
              </span>
              {(chipsExpanded ? marked : marked.slice(0, 6)).map((id) => (
                <Button
                  key={id}
                  type="button"
                  size="sm"
                  variant="secondary"
                  className="h-7 max-w-40 truncate"
                  onClick={() => {
                    const ids = multi.value.ids.filter((value) => value !== id);
                    setDrafts((current) => ({ ...current, [multi.value.mode]: ids }));
                    multi.onChange({ ...multi.value, ids });
                  }}
                >
                  {multi.items.find((item) => item.id === id)?.label ?? id} ×
                </Button>
              ))}
              {marked.length > 6 && (
                <button
                  type="button"
                  className="text-primary"
                  onClick={() => setChipsExpanded(!chipsExpanded)}
                >
                  {chipsExpanded ? "Collapse" : `and ${marked.length - 6} more · Expand`}
                </button>
              )}
            </div>
          )}
          {suggestion && (
            <div className="px-4 pt-2 text-xs text-muted-foreground">
              The other list is shorter —{" "}
              <button
                type="button"
                className="text-primary underline"
                onClick={() => {
                  setDrafts((current) => ({ ...current, [suggestion.mode]: suggestion.ids }));
                  multi.onChange(suggestion);
                }}
              >
                switch to {suggestion.mode === "only" ? "Only these" : "All, except these"} (
                {suggestion.ids.length})
              </button>
            </div>
          )}
        </>
      )}
      {showSearch && (
        <div className="px-4 py-3">
          <Input
            ref={searchRef}
            aria-label={props.searchLabel}
            placeholder={props.searchLabel}
            value={query}
            onChange={(event) => {
              setQuery(event.target.value);
              props.onQueryChange?.(event.target.value);
              setActive(0);
            }}
          />
        </div>
      )}
      {multi && (
        <div className="flex flex-wrap items-center justify-between gap-2 border-y px-4 py-2 text-xs text-muted-foreground">
          <div className="relative">
            <Button
              type="button"
              size="sm"
              variant="outline"
              onClick={() => setMenuOpen(!menuOpen)}
              aria-expanded={menuOpen}
            >
              Select ▾
            </Button>
            {menuOpen && (
              <div className="absolute left-0 top-full z-30 mt-1 min-w-44 rounded-md border bg-popover p-1 shadow-lg">
                {(["all", "none", "invert"] as const).map((action) => (
                  <button
                    key={action}
                    type="button"
                    className="block min-h-9 w-full rounded px-2 text-left hover:bg-accent"
                    onClick={() => bulk(action)}
                  >
                    {action === "all"
                      ? "Select all shown"
                      : action === "none"
                        ? "Select none shown"
                        : "Invert shown"}
                  </button>
                ))}
              </div>
            )}
          </div>
          <span>
            {query &&
              `Showing ${applicable.length} / ${multi.items.filter((item) => !item.unavailableReason).length} · `}
            Will apply {summary?.applies} / applicable {summary?.applicable} · skipped{" "}
            {summary?.skipped}
          </span>
        </div>
      )}
      <div
        ref={scrollRef}
        role={props.mode === "single" ? "listbox" : "group"}
        aria-label={props.listLabel ?? "Choices"}
        className="min-h-0 max-h-[min(440px,55dvh)] overflow-y-auto px-2 pb-3"
      >
        {props.loading}
        {props.error}
        {props.mode === "single" && props.noneLabel && (
          <button
            type="button"
            role="option"
            aria-selected={props.value === null}
            className="min-h-11 w-full rounded-lg px-3 text-left text-sm hover:bg-accent"
            onClick={() => {
              props.onChange(null);
              props.onConfirm?.();
            }}
          >
            {props.noneLabel}
          </button>
        )}
        {props.items.length > 200 ? (
          <div style={{ height: virtual.getTotalSize(), position: "relative" }}>
            {virtualItems.map((row) => (
              <div
                key={row.key}
                style={{
                  position: "absolute",
                  top: 0,
                  left: 0,
                  width: "100%",
                  transform: `translateY(${row.start}px)`,
                }}
              >
                {renderEntry(entries[row.index])}
              </div>
            ))}
          </div>
        ) : (
          groupedEntries.map((group, index) => (
            <section
              key={group.heading?.label ?? index}
              role={group.heading ? "region" : undefined}
              aria-label={group.heading?.label}
              className="px-1 py-1"
            >
              {group.heading && renderEntry(group.heading)}
              {group.rows.map((item) => (
                <div key={item.id}>{renderEntry({ kind: "row", item })}</div>
              ))}
            </section>
          ))
        )}
        {!props.loading && !props.error && entries.length === 0 && (
          <p className="px-3 py-4 text-sm text-muted-foreground">
            {props.emptyLabel ?? "No matches."}
          </p>
        )}
      </div>
    </div>
  );
}
