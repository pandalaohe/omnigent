import {
  DndContext,
  DragOverlay,
  KeyboardSensor,
  MouseSensor,
  TouchSensor,
  closestCorners,
  useSensor,
  useSensors,
  useDroppable,
  type Announcements,
  type DragEndEvent,
  type DragStartEvent,
  type DragOverEvent,
} from "@dnd-kit/core";
import {
  SortableContext,
  rectSortingStrategy,
  sortableKeyboardCoordinates,
  useSortable,
} from "@dnd-kit/sortable";
import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
  type PointerEvent as ReactPointerEvent,
} from "react";
import { toast } from "sonner";
import { MinusIcon, PinIcon, MenuIcon, PlusIcon, RotateCcwIcon, SaveIcon } from "lucide-react";

import { SettingRow } from "@/components/SettingsFields";
import { TerminalKeyContent } from "@/components/blocks/TerminalKeyContent";
import { Button } from "@/components/ui/button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Switch } from "@/components/ui/switch";
import { USER_PREFERENCES_PATCH_REJECTED_EVENT } from "@/lib/userPreferencesSync";
import {
  MAX_PREFERENCE_ENVELOPE_BYTES,
  MAX_ROW_PAGES,
  ROW_CELLS,
  defaultTerminalBar,
  packRow,
  moveBarKey,
  type BarLimitIssue,
} from "@/lib/terminalKeyBar";
import {
  readMobileAssistantPreferences,
  type ResolvedMobileAssistantPreferences,
} from "@/lib/mobileAssistantPreferences";
import {
  fromBar322,
  resolveTerminalKeys,
  validateTerminalKeySurface,
  writeTerminalKeyPreferences,
  MAX_SEND_BYTES,
  type SurfaceKeys,
  type TerminalKeyRowV2,
  type KeySurface,
} from "@/lib/terminalKeys";
import { TerminalKeySheet } from "./TerminalKeySheet";

type RowName = "top" | "bottom";
type StoredKey = TerminalKeyRowV2["keys"][number];
const SURFACE_LABELS = { shell: "Shell", native: "Native terminal" } as const;

const PREFERENCE_SIZE_ERROR = `Too large to save: preferences are limited to ${MAX_PREFERENCE_ENVELOPE_BYTES / 1024} KiB. Shorten or remove some keys.`;

function createKeyId(): string {
  return `key-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
}

function newKey(): StoredKey {
  return {
    id: createKeyId(),
    label: "",
    action: { type: "send", send: "" },
    display: "text",
    width: 2,
  };
}

function rowClone(row: TerminalKeyRowV2): TerminalKeyRowV2 {
  return { ...row, keys: [...row.keys] };
}

function withRow(bar: SurfaceKeys, row: RowName, next: TerminalKeyRowV2): SurfaceKeys {
  return row === "top" ? { ...bar, top: next } : { ...bar, bottom: next };
}

function withRows(layout: SurfaceKeys, rows: SurfaceKeys): SurfaceKeys {
  return { ...layout, top: rows.top, bottom: rows.bottom };
}

function PageEndDropZone({
  row,
  index,
  cells,
  children,
}: {
  row: RowName;
  index: number;
  cells: number;
  children: ReactNode;
}) {
  const { setNodeRef } = useDroppable({
    id: `${row}:end:${index}`,
    data: { row, index, boundary: true },
  });
  return (
    <div
      ref={setNodeRef}
      data-drop-end={`${row}:${index}`}
      className="grid h-11 gap-[2px]"
      style={{
        gridColumn: `span ${cells}`,
        gridTemplateColumns: `repeat(${cells}, minmax(0,1fr))`,
      }}
    >
      {children}
    </div>
  );
}

function limitMessage(issue: BarLimitIssue, row: TerminalKeyRowV2): string {
  if (issue.kind === "pages") return "This row is full: 5 pages";
  if (issue.kind === "label") return "Labels can be at most 12 characters";
  if (issue.kind === "text") {
    const action = row.keys.find((key) => key.id === issue.keyId)?.action;
    return action?.type === "send"
      ? `The decoded sequence can be at most ${MAX_SEND_BYTES} bytes`
      : "Legacy text can be at most 64 characters";
  }
  if (issue.kind === "id") return "Key id is too long";
  return "This key is too large";
}

function structuralMessage(value: string): string {
  if (value.includes("no room")) return "Pinned keys leave no room for the rest";
  if (value.includes("pinned keys exceed") || value.includes("pinned"))
    return "Pinned keys can fill at most 12 cells";
  return "This row could not be read";
}

function SortableKeyTile({
  barKey,
  row,
  index,
  pinned,
  onOpen,
}: {
  barKey: StoredKey;
  row: RowName;
  index: number;
  pinned: boolean;
  onOpen: () => void;
}) {
  const sortable = useSortable({
    id: barKey.id,
    data: { row, index, keyId: barKey.id },
  });
  const style = {
    transform: sortable.transform
      ? `translate3d(${Math.round(sortable.transform.x)}px, ${Math.round(sortable.transform.y)}px, 0) scaleX(${sortable.transform.scaleX}) scaleY(${sortable.transform.scaleY})`
      : undefined,
    transition: sortable.transition,
    gridColumn: `span ${barKey.width}`,
    opacity: sortable.isDragging ? 0.45 : 1,
  };
  return (
    <div
      ref={sortable.setNodeRef}
      style={style}
      className="relative min-w-0 motion-reduce:transition-none!"
    >
      <button
        type="button"
        ref={sortable.setActivatorNodeRef}
        {...sortable.attributes}
        {...sortable.listeners}
        data-editor-key={barKey.id}
        aria-label={`Edit ${barKey.label}, ${barKey.width} ${barKey.width === 1 ? "cell" : "cells"}${pinned ? ", pinned" : ""}`}
        onClick={(event) => {
          event.preventDefault();
          onOpen();
        }}
        className={
          pinned
            ? "flex h-11 w-full min-w-0 items-center justify-center gap-1 rounded-[7px] border border-primary/50 bg-primary/10 px-1 font-mono text-[12px] font-semibold text-foreground"
            : "flex h-11 w-full min-w-0 items-center justify-center gap-1 rounded-[7px] border border-border bg-background px-1 font-mono text-[12px] font-semibold text-foreground"
        }
      >
        <TerminalKeyContent button={barKey} className="size-3.5 shrink-0" />
        {pinned ? (
          <PinIcon className="absolute right-0.5 top-0.5 size-2 shrink-0" aria-hidden />
        ) : null}
      </button>
    </div>
  );
}

function PinnedCopy({ barKey }: { barKey: StoredKey }) {
  return (
    <div
      style={{ gridColumn: `span ${barKey.width}` }}
      aria-hidden="true"
      className="flex h-11 min-w-0 items-center justify-center truncate rounded-[7px] border border-dashed border-border bg-muted/30 px-1 font-mono text-[12px] font-semibold text-muted-foreground opacity-60"
    >
      <TerminalKeyContent button={barKey} className="size-3.5 shrink-0" />
    </div>
  );
}

export interface TerminalBarEditorProps {
  preferences: ResolvedMobileAssistantPreferences;
}

export function TerminalBarEditor({ preferences }: TerminalBarEditorProps) {
  const resolved = useMemo(() => resolveTerminalKeys({ ...preferences }), [preferences]);
  return (
    <Tabs defaultValue="shell" className="mt-4">
      <TabsList aria-label="Terminal key bar page" className="h-11!">
        <TabsTrigger value="shell" className="px-3">
          Shell
        </TabsTrigger>
        <TabsTrigger value="native" className="px-3">
          Native terminal
        </TabsTrigger>
      </TabsList>
      {(["shell", "native"] as const).map((surface) => (
        <TabsContent value={surface} key={surface}>
          <SurfaceEditor
            surface={surface}
            layout={resolved.keys[surface]}
            preset={resolved.preset[surface]}
            fallback={resolved.source === "fallback"}
          />
        </TabsContent>
      ))}
    </Tabs>
  );
}

function SurfaceEditor({
  surface,
  layout,
  preset,
  fallback,
}: {
  surface: KeySurface;
  layout: SurfaceKeys;
  preset?: SurfaceKeys | "invalid";
  fallback: boolean;
}) {
  const [bar, setBar] = useState(layout);
  const otherSurface = surface === "shell" ? "native" : "shell";
  const [rowError, setRowError] = useState<{ row: RowName; message: string } | null>(null);
  const [sheet, setSheet] = useState<{
    key: StoredKey;
    isNew: boolean;
    row: RowName;
    index: number;
    error: string | null;
  } | null>(null);
  const [rejection, setRejection] = useState(false);
  const [draggingId, setDraggingId] = useState<string | null>(null);
  const dragStartedRef = useRef(false);
  const dragBarRef = useRef<SurfaceKeys | null>(null);
  const lastErrorRef = useRef("");
  const pinDragRef = useRef<{
    row: RowName;
    pointerId: number;
    startX: number;
    startPinned: number;
    cellWidth: number;
  } | null>(null);

  useEffect(() => {
    setBar(layout);
  }, [layout]);

  useEffect(() => {
    const onRejected = (event: Event) => {
      const detail = (event as CustomEvent<{ namespace?: string }>).detail;
      if (detail?.namespace !== "mobile_assistant") return;
      setRejection(true);
    };
    window.addEventListener(USER_PREFERENCES_PATCH_REJECTED_EVENT, onRejected);
    return () => window.removeEventListener(USER_PREFERENCES_PATCH_REJECTED_EVENT, onRejected);
  }, []);

  const sensors = useSensors(
    useSensor(MouseSensor, { activationConstraint: { distance: 5 } }),
    useSensor(TouchSensor, { activationConstraint: { delay: 250, tolerance: 8 } }),
    useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates }),
  );

  const persist = (
    candidate: SurfaceKeys,
    options: { before?: SurfaceKeys; message?: string; savePreset?: boolean } = {},
  ): boolean => {
    const current = readMobileAssistantPreferences();
    const { keys, preset: presets } = resolveTerminalKeys({ ...current });
    const validation = validateTerminalKeySurface(candidate, keys[surface]);
    const structural = validation.structural[0];
    const worse = validation.limits.find((issue) => issue.worse);
    if (structural || worse) {
      lastErrorRef.current = worse
        ? limitMessage(worse, candidate[worse.row])
        : structuralMessage(structural);
      setRowError({
        row: worse?.row ?? (structural.startsWith("bottom") ? "bottom" : "top"),
        message: lastErrorRef.current,
      });
      return false;
    }
    const result = writeTerminalKeyPreferences({
      terminalKeys: { ...keys, [surface]: candidate },
      ...(options.savePreset
        ? {
            terminalKeysPreset: {
              ...current.terminalKeysPreset,
              ...(presets.shell && presets.shell !== "invalid" ? { shell: presets.shell } : {}),
              [surface]: candidate,
            },
          }
        : {}),
    });
    if (result !== "saved") {
      lastErrorRef.current =
        result === "too-large" ? PREFERENCE_SIZE_ERROR : "Couldn't save terminal keys";
      setRowError({ row: "top", message: lastErrorRef.current });
      return false;
    }
    setBar(candidate);
    setRowError(null);
    setRejection(false);
    if (options.message) {
      const undo = options.before;
      toast(options.message, {
        duration: 6000,
        ...(undo
          ? {
              action: {
                label: "Undo",
                onClick: () => {
                  const latest = resolveTerminalKeys({ ...readMobileAssistantPreferences() }).keys[
                    surface
                  ];
                  if (
                    JSON.stringify(latest.top) !== JSON.stringify(candidate.top) ||
                    JSON.stringify(latest.bottom) !== JSON.stringify(candidate.bottom)
                  ) {
                    toast.error("This layout has changed since that action");
                    return;
                  }
                  if (persist(withRows(latest, undo))) toast("Layout restored");
                },
              },
            }
          : {}),
      });
    }
    return true;
  };

  const openSheet = (key: StoredKey, isNew: boolean, row: RowName, index: number) =>
    setSheet({ key: structuredClone(key), isNew, row, index, error: null });

  const saveSheet = (key: StoredKey) => {
    if (!sheet) return;
    const rowName = sheet.isNew
      ? sheet.row
      : bar.top.keys.some((item) => item.id === key.id)
        ? "top"
        : "bottom";
    const target = rowClone(bar[rowName]);
    if (sheet.isNew) {
      target.keys.splice(Math.min(sheet.index, target.keys.length), 0, key);
    } else {
      const at = target.keys.findIndex((item) => item.id === key.id);
      if (at < 0) {
        setSheet({ ...sheet, error: "This key was removed from the layout." });
        return;
      }
      target.keys[at] = key;
    }
    if (persist(withRow(bar, rowName, target))) setSheet(null);
    else setSheet((current) => (current ? { ...current, error: lastErrorRef.current } : current));
  };

  const deleteKey = (id: string) => {
    const rowName = bar.top.keys.some((key) => key.id === id) ? "top" : "bottom";
    const previous = bar;
    const target = rowClone(bar[rowName]);
    const removed = target.keys.find((key) => key.id === id);
    if (!removed) return;
    if (target.keys.findIndex((key) => key.id === id) < target.pinned) target.pinned -= 1;
    target.keys = target.keys.filter((key) => key.id !== id);
    target.pinned = Math.min(target.pinned, target.keys.length);
    const candidate = withRow(bar, rowName, target);
    if (persist(candidate, { before: previous, message: `Deleted ${removed.label}` }))
      setSheet(null);
    else setSheet((current) => (current ? { ...current, error: lastErrorRef.current } : current));
  };

  const moveKey = (id: string, direction: "left" | "right" | "other-row") => {
    const rowName = bar.top.keys.some((key) => key.id === id) ? "top" : "bottom";
    const from = bar[rowName].keys.findIndex((key) => key.id === id);
    if (from < 0) return;
    const targetRow: RowName =
      direction === "other-row" ? (rowName === "top" ? "bottom" : "top") : rowName;
    const to =
      direction === "other-row"
        ? bar[targetRow].keys.length
        : Math.max(
            0,
            Math.min(bar[rowName].keys.length - 1, from + (direction === "left" ? -1 : 1)),
          );
    if (targetRow === rowName && to === from) return;
    persist(
      moveBarKey(
        bar,
        id,
        targetRow,
        to + (targetRow === rowName && from < to ? 1 : 0),
        targetRow === rowName && to < bar[rowName].pinned,
      ),
    );
  };

  const setPinned = (rowName: RowName, nextPinned: number) => {
    const target = rowClone(bar[rowName]);
    target.pinned = Math.max(0, Math.min(nextPinned, target.keys.length));
    persist(withRow(bar, rowName, target));
  };

  const startPinDrag = (rowName: RowName, event: ReactPointerEvent<HTMLButtonElement>) => {
    event.preventDefault();
    const grid = event.currentTarget.closest<HTMLElement>("[data-key-grid]");
    const width = grid?.getBoundingClientRect().width ?? 0;
    pinDragRef.current = {
      row: rowName,
      pointerId: event.pointerId,
      startX: event.clientX,
      startPinned: bar[rowName].pinned,
      cellWidth: width > 0 ? width / ROW_CELLS : 1,
    };
    event.currentTarget.setPointerCapture?.(event.pointerId);
  };

  const movePinDrag = (event: ReactPointerEvent<HTMLButtonElement>) => {
    const drag = pinDragRef.current;
    if (!drag || drag.pointerId !== event.pointerId) return;
    const row = bar[drag.row];
    const startCells = row.keys.slice(0, drag.startPinned).reduce((sum, key) => sum + key.width, 0);
    const targetCell = startCells + (event.clientX - drag.startX) / drag.cellWidth;
    let cells = 0;
    let next = 0;
    for (const key of row.keys) {
      if (targetCell < cells + key.width / 2) break;
      cells += key.width;
      next += 1;
    }
    setPinned(drag.row, next);
  };

  const finishPinDrag = (event: ReactPointerEvent<HTMLButtonElement>) => {
    const drag = pinDragRef.current;
    if (!drag || drag.pointerId !== event.pointerId) return;
    pinDragRef.current = null;
  };

  const onDragStart = (event: DragStartEvent) => {
    dragStartedRef.current = true;
    dragBarRef.current = bar;
    setDraggingId(String(event.active.id));
  };

  const dropPosition = (event: DragOverEvent | DragEndEvent) => {
    const to = event.over?.data.current as
      { row: RowName; index: number; keyId?: string; boundary?: boolean } | undefined;
    if (!to || !event.over) return null;
    const row = bar[to.row];
    let boundary = to.index;
    let pinned = false;
    if (!to.boundary) {
      const rect = event.active.rect.current.translated;
      const center = rect ? rect.left + rect.width / 2 : event.over.rect.left;
      if (event.activatorEvent.type === "keydown") {
        const from = row.keys.findIndex((key) => key.id === String(event.active.id));
        if (from >= 0 && from < to.index) boundary += 1;
        pinned = to.index < row.pinned;
      } else {
        if (center > event.over.rect.left + event.over.rect.width / 2) boundary += 1;
        const tile = to.keyId
          ? document.querySelector(`[data-editor-key="${CSS.escape(to.keyId)}"]`)
          : null;
        const grid = tile?.closest<HTMLElement>("[data-key-grid]");
        const box = grid?.getBoundingClientRect();
        const pinCells = row.keys.slice(0, row.pinned).reduce((sum, key) => sum + key.width, 0);
        pinned = box
          ? center < box.left + (box.width * pinCells) / ROW_CELLS
          : boundary < row.pinned;
      }
    }
    return { row: to.row, boundary, pinned };
  };

  const onDragOver = (event: DragOverEvent) => {
    if (!dragBarRef.current) return;
    const id = String(event.active.id);
    const fromRow = bar.top.keys.some((key) => key.id === id) ? "top" : "bottom";
    const to = dropPosition(event);
    if (!to || to.row === fromRow) return;
    const candidate = moveBarKey(bar, id, to.row, to.boundary, to.pinned);
    const validation = validateTerminalKeySurface(candidate, dragBarRef.current);
    if (!validation.structural.length && !validation.limits.some((issue) => issue.worse))
      setBar(candidate);
  };

  const onDragEnd = (event: DragEndEvent) => {
    setDraggingId(null);
    window.setTimeout(() => {
      dragStartedRef.current = false;
    }, 0);
    const before = dragBarRef.current;
    dragBarRef.current = null;
    if (!before) return;
    const id = String(event.active.id);
    const to = dropPosition(event);
    if (!to || !event.over) {
      setBar(before);
      return;
    }
    if (String(event.over.id) === id) {
      if (bar !== before) persist(bar);
      return;
    }
    const candidate = moveBarKey(bar, id, to.row, to.boundary, to.pinned);
    if (!persist(candidate)) setBar(before);
  };

  const dragLabel = (id: string | number) =>
    [...bar.top.keys, ...bar.bottom.keys].find((key) => key.id === String(id))?.label ?? "page end";
  const announcements: Announcements = {
    onDragStart: ({ active }) => `Picked up ${dragLabel(active.id)}.`,
    onDragOver: ({ over }) => (over ? `Over ${dragLabel(over.id)}.` : "Not over a key."),
    onDragEnd: ({ over }) => (over ? "Key moved." : "Key returned to its place."),
    onDragCancel: () => "Move cancelled.",
  };

  const allIds = [...bar.top.keys, ...bar.bottom.keys].map((key) => key.id);

  const renderRow = (rowName: RowName, row: TerminalKeyRowV2) => {
    const pack = packRow(row);
    const pages = pack.pages;
    const pageNumbers = pages.map((_, index) => index + 1);
    const rowLabel = rowName === "top" ? "Top row" : "Bottom row";
    return (
      <section key={rowName} className="mt-4" aria-label={rowLabel}>
        <div className="flex flex-wrap items-center gap-2">
          <h4 className="text-ui font-medium">
            {rowLabel} · {pages.length === 1 ? "1 page" : `${pages.length} pages`}
            {pages.length > MAX_ROW_PAGES ? ` · limit ${MAX_ROW_PAGES}` : ""}
          </h4>
          {rowName === "bottom" ? (
            <label className="flex items-center gap-2 text-sm">
              <Switch
                checked={row.enabled}
                onCheckedChange={(enabled) => {
                  const target = rowClone(row);
                  target.enabled = enabled;
                  persist(withRow(bar, rowName, target));
                }}
                aria-label="Show bottom terminal key row"
              />
              On
            </label>
          ) : null}
          <span className="ml-auto flex items-center gap-1 text-sm text-muted-foreground">
            Pin first
            <Button
              type="button"
              variant="outline"
              size="icon-xs"
              aria-label={`Unpin one ${rowLabel.toLowerCase()} key`}
              disabled={row.pinned === 0}
              onClick={() => setPinned(rowName, row.pinned - 1)}
            >
              <MinusIcon />
            </Button>
            <span data-testid={`terminal-bar-pinned-${rowName}`}>{row.pinned}</span>
            <Button
              type="button"
              variant="outline"
              size="icon-xs"
              aria-label={`Pin one more ${rowLabel.toLowerCase()} key`}
              onClick={() => setPinned(rowName, row.pinned + 1)}
            >
              <PlusIcon />
            </Button>
          </span>
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={() => openSheet(newKey(), true, rowName, row.keys.length)}
          >
            <PlusIcon /> Add key
          </Button>
        </div>
        {rowError?.row === rowName ? (
          <p role="status" className="mt-2 text-sm text-destructive">
            {rowError.message}
          </p>
        ) : null}
        <div className="mt-2 grid gap-2">
          {pageNumbers.map((pageNumber) => {
            const pageIndex = pageNumber - 1;
            const pageKeys = pages[pageIndex];
            const used = pageKeys.reduce((total, key) => total + key.width, 0);
            const gaps = Math.max(0, pack.pagerCells - used);
            return (
              <div
                key={`${rowName}-page-${pageNumber}`}
                className="rounded-lg border border-border/70 p-2"
              >
                <p className="mb-1 text-sm text-muted-foreground">
                  page {pageIndex + 1} · {ROW_CELLS - gaps} cells
                </p>
                <div
                  data-key-grid
                  className={`relative grid grid-cols-12 gap-[2px]${pageIndex === 0 ? " pt-5" : ""}`}
                >
                  {pageIndex === 0
                    ? pack.pinned.map((key, index) => (
                        <SortableKeyTile
                          key={`${rowName}-pinned-${key.id}`}
                          barKey={key}
                          row={rowName}
                          index={index}
                          pinned
                          onOpen={() => {
                            if (!dragStartedRef.current) openSheet(key, false, rowName, index);
                          }}
                        />
                      ))
                    : pack.pinned.map((key) => (
                        <PinnedCopy key={`${rowName}-copy-${key.id}`} barKey={key} />
                      ))}
                  {pageIndex === 0 ? (
                    <button
                      type="button"
                      data-pin-handle
                      aria-label={`Drag to change how many ${rowLabel.toLowerCase()} keys are pinned`}
                      onPointerDown={(event) => startPinDrag(rowName, event)}
                      onPointerMove={movePinDrag}
                      onPointerUp={finishPinDrag}
                      onPointerCancel={finishPinDrag}
                      onLostPointerCapture={finishPinDrag}
                      style={{
                        left: `clamp(0.625rem, ${(pack.pinned.reduce((sum, key) => sum + key.width, 0) / ROW_CELLS) * 100}%, calc(100% - 0.625rem))`,
                      }}
                      className="absolute top-0 z-10 flex h-4 w-5 -translate-x-1/2 items-center justify-center rounded border border-border bg-card text-muted-foreground touch-none"
                    >
                      <MenuIcon className="size-4" aria-hidden />
                    </button>
                  ) : null}
                  {pageKeys.map((key) => {
                    const absoluteIndex = row.keys.findIndex((item) => item.id === key.id);
                    return (
                      <SortableKeyTile
                        key={`${rowName}-${pageIndex}-${key.id}`}
                        barKey={key}
                        row={rowName}
                        index={absoluteIndex}
                        pinned={false}
                        onOpen={() => {
                          if (!dragStartedRef.current)
                            openSheet(key, false, rowName, absoluteIndex);
                        }}
                      />
                    );
                  })}
                  {gaps > 0 ? (
                    <PageEndDropZone
                      row={rowName}
                      index={
                        row.pinned +
                        pages.slice(0, pageIndex + 1).reduce((sum, page) => sum + page.length, 0)
                      }
                      cells={gaps}
                    >
                      <button
                        type="button"
                        aria-label={`Add key at ${rowLabel.toLowerCase()} page ${pageIndex + 1}`}
                        onClick={() =>
                          openSheet(
                            newKey(),
                            true,
                            rowName,
                            row.pinned +
                              pages
                                .slice(0, pageIndex + 1)
                                .reduce((sum, page) => sum + page.length, 0),
                          )
                        }
                        style={{ gridColumn: "span 1" }}
                        className="flex h-11 items-center justify-center rounded-[7px] border border-dashed border-border text-muted-foreground"
                      >
                        <PlusIcon className="size-4" aria-hidden />
                      </button>
                    </PageEndDropZone>
                  ) : null}
                </div>
              </div>
            );
          })}
        </div>
      </section>
    );
  };

  const sheetRowKeys = bar.top.keys.some((key) => key.id === sheet?.key.id)
    ? bar.top.keys
    : bar.bottom.keys;
  const sheetIndex = sheetRowKeys.findIndex((key) => key.id === sheet?.key.id);

  const restoreDefaults = () =>
    persist(withRows(bar, fromBar322(defaultTerminalBar())), {
      before: bar,
      message: "Restored default keys",
    });

  return (
    <div className="space-y-3 phone:[&_[data-slot=setting-row]]:min-h-11">
      <SettingRow label="Show key bar on this page">
        <Switch
          checked={bar.enabled}
          onCheckedChange={(enabled) => persist({ ...bar, enabled })}
          aria-label="Show key bar on this page"
        />
      </SettingRow>
      <SettingRow label="Only while the phone keyboard is up">
        <Switch
          checked={bar.showRows === "keyboard"}
          onCheckedChange={(onlyWithKeyboard) => {
            const { showRows: _showRows, ...withoutKeyboardGate } = bar;
            persist(
              onlyWithKeyboard
                ? { ...withoutKeyboardGate, showRows: "keyboard" }
                : withoutKeyboardGate,
            );
          }}
          aria-label="Only while the phone keyboard is up"
        />
      </SettingRow>
      {fallback ? (
        <p role="status" className="rounded-md border border-border bg-muted/40 px-3 py-2 text-sm">
          Saved terminal key layouts couldn’t be read. Your next edit saves the layout shown here.
        </p>
      ) : null}
      {rejection ? (
        <p
          role="alert"
          className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm"
        >
          Couldn't sync the key layout. Review the layout shown here and save your changes again.
        </p>
      ) : null}

      <DndContext
        sensors={sensors}
        collisionDetection={closestCorners}
        accessibility={{ announcements }}
        onDragStart={onDragStart}
        onDragOver={onDragOver}
        onDragEnd={onDragEnd}
        onDragCancel={() => {
          setDraggingId(null);
          if (dragBarRef.current) setBar(dragBarRef.current);
          dragBarRef.current = null;
          window.setTimeout(() => {
            dragStartedRef.current = false;
          }, 0);
        }}
      >
        <SortableContext items={allIds} strategy={rectSortingStrategy}>
          {renderRow("top", bar.top)}
          {renderRow("bottom", bar.bottom)}
        </SortableContext>
        <DragOverlay dropAnimation={null}>
          {draggingId ? (
            <div className="flex h-11 items-center justify-center rounded-[7px] border border-primary bg-popover px-1 font-mono text-xs text-popover-foreground shadow-lg">
              {[...bar.top.keys, ...bar.bottom.keys].find((key) => key.id === draggingId)?.label}
            </div>
          ) : null}
        </DragOverlay>
      </DndContext>

      <div className="flex flex-wrap items-center gap-2 phone:gap-y-4">
        <Button type="button" variant="outline" onClick={restoreDefaults}>
          <RotateCcwIcon /> Restore defaults
        </Button>
        <Button
          type="button"
          variant="outline"
          onClick={() => {
            if (persist(bar, { savePreset: true })) toast("Preset saved");
          }}
        >
          <SaveIcon /> Save as my preset
        </Button>
        {preset && preset !== "invalid" ? (
          <Button
            type="button"
            variant="outline"
            onClick={() =>
              persist(withRows(bar, preset), { before: bar, message: "Loaded my preset" })
            }
          >
            Load my preset
          </Button>
        ) : null}
        {preset === "invalid" ? (
          <span className="self-center text-sm text-muted-foreground">
            Saved preset couldn't be read
          </span>
        ) : null}
        <Button
          type="button"
          variant="outline"
          onClick={() => {
            const { keys } = resolveTerminalKeys({ ...readMobileAssistantPreferences() });
            persist(withRows(keys[surface], keys[otherSurface]), {
              before: bar,
              message: `Copied layout from ${SURFACE_LABELS[otherSurface]}`,
            });
          }}
        >
          Copy from {SURFACE_LABELS[otherSurface]}
        </Button>
      </div>

      {sheet ? (
        <TerminalKeySheet
          key={sheet.key.id}
          keyDraft={sheet.key}
          isNew={sheet.isNew}
          canMoveLeft={sheetIndex > 0}
          canMoveRight={sheetIndex >= 0 && sheetIndex < sheetRowKeys.length - 1}
          error={sheet.error}
          onClose={() => setSheet(null)}
          onSave={saveSheet}
          onDelete={deleteKey}
          onMove={moveKey}
        />
      ) : null}
      <span className="sr-only" aria-live="polite">
        {draggingId ? "Moving key" : ""}
      </span>
    </div>
  );
}
