import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
  type ReactNode,
} from "react";
import {
  DndContext,
  DragOverlay,
  pointerWithin,
  useDroppable,
  useSensor,
  useSensors,
  type DragMoveEvent,
} from "@dnd-kit/core";
import { useSortable } from "@dnd-kit/sortable";
import { createPortal } from "react-dom";
import { overlayAtPointer } from "./choiceOverlay";
import { ChoiceMouseSensor, ChoiceTouchSensor } from "./choiceSensors";
import { GripVerticalIcon } from "lucide-react";
import { cn } from "@/lib/utils";
import {
  resolveDropIntent,
  type ChoiceDragData,
  type ChoiceDragItem,
  type DropIntent,
} from "./choiceDrop";

export type { ChoiceDragData, DropIntent } from "./choiceDrop";
type Registered = ChoiceDragItem & { node: HTMLElement; container: boolean };
interface DragContextValue {
  intent: DropIntent | null;
  active: string | null;
  register: (item: ChoiceDragItem, node: HTMLElement | null, container: boolean) => void;
  keyboard: (event: KeyboardEvent, item: ChoiceDragItem) => void;
  instructions: string;
}
const DragContext = createContext<DragContextValue | null>(null);
const EMPTY_LEVELS: string[][] = [];
const QUIET_ANNOUNCEMENTS = {
  onDragStart: () => undefined,
  onDragOver: () => undefined,
  onDragEnd: () => undefined,
  onDragCancel: () => undefined,
};

export function ChoiceDragContext({
  children,
  abilityOrder = EMPTY_LEVELS,
  reset,
  scope,
  onDrop,
}: {
  children: ReactNode;
  abilityOrder?: string[][];
  reset: unknown;
  scope?: string;
  onDrop: (intent: DropIntent) => { focus: string; message: string };
}) {
  const instructions = useId();
  const registry = useRef(new Map<string, Registered>());
  const [source, setSource] = useState<ChoiceDragItem | null>(null);
  const sourceRef = useRef<ChoiceDragItem | null>(null);
  const inputMode = useRef<"pointer" | "keyboard" | null>(null);
  const pointerY = useRef<number | null>(null);
  const overlayOrigin = useRef<DOMRect | null>(null);
  const [intent, setIntent] = useState<DropIntent | null>(null);
  const intentRef = useRef<DropIntent | null>(null);
  const [status, setStatus] = useState("");
  const cursor = useRef<{ group: number; slot: number } | null>(null);
  const cancelPointer = useRef<(() => void) | null>(null);
  const sensorReady = useCallback((cancel: (() => void) | null) => {
    cancelPointer.current = cancel;
  }, []);
  const sensors = useSensors(
    useSensor(ChoiceMouseSensor, { activationConstraint: { distance: 5 }, onReady: sensorReady }),
    useSensor(ChoiceTouchSensor, {
      activationConstraint: { delay: 250, tolerance: 8 },
      onReady: sensorReady,
    }),
  );
  const focus = useCallback((id: string) => {
    requestAnimationFrame(() =>
      registry.current
        .get(id)
        ?.node.querySelector<HTMLButtonElement>("[data-choice-handle]")
        ?.focus(),
    );
  }, []);
  const clear = useCallback(
    (message: string, focusId?: string) => {
      setSource(null);
      sourceRef.current = null;
      inputMode.current = null;
      setIntent(null);
      intentRef.current = null;
      cursor.current = null;
      setStatus(message);
      if (focusId) focus(focusId);
    },
    [focus],
  );
  useEffect(() => {
    const active = sourceRef.current;
    cancelPointer.current?.();
    if (active) {
      clear("Move cancelled: policy changed.", active.id);
    }
  }, [reset, scope, clear]);
  useEffect(() => () => cancelPointer.current?.(), []);
  useEffect(() => {
    if (!source) return;
    const sourceId = source.id;
    function cancel(restoreFocus: boolean) {
      const active = sourceRef.current;
      if (!active) return;
      cancelPointer.current?.();
      clear("Move cancelled.", restoreFocus ? active.id : undefined);
    }
    function keydown(event: globalThis.KeyboardEvent) {
      if (event.key === "Escape") {
        event.preventDefault();
        cancel(true);
      } else if (event.key === "Tab") cancel(false);
    }
    function focusChanged(event: FocusEvent) {
      const handle = registry.current.get(sourceId)?.node.querySelector("[data-choice-handle]");
      if (inputMode.current === "keyboard" && event.target !== handle) cancel(false);
    }
    const blurred = () => cancel(false);
    document.addEventListener("keydown", keydown);
    document.addEventListener("focusin", focusChanged);
    window.addEventListener("blur", blurred);
    return () => {
      document.removeEventListener("keydown", keydown);
      document.removeEventListener("focusin", focusChanged);
      window.removeEventListener("blur", blurred);
    };
  }, [source, clear]);
  const show = useCallback((next: DropIntent | null) => {
    intentRef.current = next;
    setIntent((previous) =>
      previous?.source.id === next?.source.id &&
      previous?.target.id === next?.target.id &&
      previous?.edge === next?.edge &&
      previous?.message === next?.message
        ? previous
        : next,
    );
    setStatus(next?.message ?? "Outside drop targets. Release to cancel.");
  }, []);
  const finish = useCallback(
    (active: ChoiceDragItem) => {
      const current = intentRef.current;
      if (!current?.valid) {
        clear(current?.message ?? "Move cancelled.", active.id);
        return;
      }
      const result = onDrop(current);
      clear(result.message, result.focus);
    },
    [onDrop, clear],
  );
  function moved(event: DragMoveEvent) {
    if (!sourceRef.current) return;
    const active = registry.current.get(String(event.active.id));
    const target = event.over && registry.current.get(String(event.over.id));
    if (!active || !target) {
      show(null);
      return;
    }
    const rect = event.over!.rect;
    const y = pointerY.current;
    if (y === null) {
      show(null);
      return;
    }
    const data = target.data;
    const empty =
      (data.kind === "tier" || data.kind === "ability-level") &&
      ![...registry.current.values()].some((entry) =>
        data.kind === "tier"
          ? entry.data.kind === "row" && entry.data.tier === data.tier
          : entry.data.kind === "ability" && entry.data.level === data.level,
      );
    show(
      resolveDropIntent(
        active,
        target,
        empty ? "empty" : y < rect.top + rect.height / 2 ? "before" : "after",
        abilityOrder,
      ),
    );
  }
  const keyboard = useCallback(
    (event: KeyboardEvent, item: ChoiceDragItem) => {
      if (!source) {
        if (event.key !== " " && event.key !== "Enter") return;
        event.preventDefault();
        setSource(item);
        sourceRef.current = item;
        inputMode.current = "keyboard";
        cursor.current = null;
        setStatus(
          `Picked up ${item.label}. Use arrows, Home or End to choose a position. Enter to drop; Escape to cancel.`,
        );
        return;
      }
      if (event.key === "Escape") {
        event.preventDefault();
        clear("Move cancelled.", source.id);
        return;
      }
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        finish(source);
        return;
      }
      if (!["ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key))
        return;
      event.preventDefault();
      const nodes = [...registry.current.values()].sort((a, b) =>
        a.node.compareDocumentPosition(b.node) & Node.DOCUMENT_POSITION_FOLLOWING ? -1 : 1,
      );
      const groups = new Map<string, Registered[]>();
      for (const target of nodes) {
        if (!resolveDropIntent(source, target, "before", abilityOrder).valid) continue;
        const d = target.data;
        const key =
          d.kind === "row" || d.kind === "tier"
            ? `tier:${d.tier}`
            : d.kind === "ability" || d.kind === "ability-level"
              ? `ability:${d.level}`
              : d.kind;
        groups.set(key, [...(groups.get(key) ?? []), target]);
      }
      const lists = [...groups.values()];
      if (!lists.length) {
        show(null);
        setStatus("No available drop targets.");
        return;
      }
      const slots = lists.map((list) => {
        const items = list.filter((entry) => !entry.container && entry.id !== source.id);
        if (!items.length)
          return [
            { target: list.find((entry) => entry.container) ?? list[0], edge: "empty" as const },
          ];
        return [
          { target: items[0], edge: "before" as const },
          ...items.map((target) => ({ target, edge: "after" as const })),
        ];
      });
      let { group, slot } = cursor.current ?? {
        group: Math.max(
          0,
          lists.findIndex((list) => list.some((entry) => entry.id === source.id)),
        ),
        slot: 0,
      };
      if (!cursor.current)
        slot = Math.max(
          0,
          lists[group]
            .filter((entry) => !entry.container)
            .findIndex((entry) => entry.id === source.id),
        );
      if (event.key === "ArrowLeft" || event.key === "ArrowRight")
        group = Math.max(
          0,
          Math.min(lists.length - 1, group + (event.key === "ArrowLeft" ? -1 : 1)),
        );
      if (event.key === "ArrowUp") slot--;
      if (event.key === "ArrowDown") slot++;
      if (event.key === "Home") slot = 0;
      if (event.key === "End") slot = slots[group].length - 1;
      slot = Math.max(0, Math.min(slots[group].length - 1, slot));
      cursor.current = { group, slot };
      const target = slots[group][slot];
      const details = target.target.node.closest("details");
      if (details) details.open = true;
      target.target.node.scrollIntoView?.({ block: "nearest", inline: "nearest" });
      show(resolveDropIntent(source, target.target, target.edge, abilityOrder));
    },
    [source, abilityOrder, clear, show, finish],
  );
  const contextValue = useMemo(
    () => ({
      intent,
      active: source?.id ?? null,
      instructions,
      keyboard,
      register: (item: ChoiceDragItem, node: HTMLElement | null, container: boolean) => {
        if (node) registry.current.set(item.id, { ...item, node, container });
        else registry.current.delete(item.id);
      },
    }),
    [intent, source, instructions, keyboard],
  );
  return (
    <DragContext.Provider value={contextValue}>
      <DndContext
        accessibility={{ announcements: QUIET_ANNOUNCEMENTS, restoreFocus: false }}
        sensors={sensors}
        collisionDetection={(args) => {
          pointerY.current = args.pointerCoordinates?.y ?? null;
          return pointerWithin(args).sort(
            (a, b) =>
              Number(registry.current.get(String(a.id))?.container) -
              Number(registry.current.get(String(b.id))?.container),
          );
        }}
        onDragStart={(event) => {
          const item = registry.current.get(String(event.active.id));
          if (item) {
            overlayOrigin.current = item.node.getBoundingClientRect();
            setSource(item);
            sourceRef.current = item;
            inputMode.current = "pointer";
            setStatus(`Picked up ${item.label}.`);
          }
        }}
        onDragMove={moved}
        onDragOver={moved}
        onDragEnd={(event) => {
          const item = registry.current.get(String(event.active.id));
          if (item) finish(item);
        }}
        onDragCancel={() => clear("Move cancelled.")}
      >
        {children}
        {createPortal(
          <DragOverlay
            modifiers={[
              (args) =>
                overlayAtPointer({
                  ...args,
                  activeNodeRect: overlayOrigin.current ?? args.activeNodeRect,
                }),
            ]}
            dropAnimation={null}
            style={{ pointerEvents: "none" }}
          >
            {source && (
              <div className="max-w-64 rounded-md border border-primary bg-background px-3 py-2 text-sm shadow-lg">
                {source.data.kind === "preset" ? "Copy" : "Move"} {source.label}
              </div>
            )}
          </DragOverlay>,
          document.body,
        )}
      </DndContext>
      <p id={instructions} className="sr-only">
        Space or Enter to pick up. Up and Down choose a position; Left and Right change lists. Home
        and End go to the ends. Enter to drop; Escape to cancel.
      </p>
      {status && (
        <p
          role="status"
          aria-label="Drag result"
          aria-live="polite"
          className="min-h-5 break-words text-sm text-muted-foreground"
        >
          {status}
        </p>
      )}
    </DragContext.Provider>
  );
}

function DropCue({ id }: { id: string }) {
  const context = useContext(DragContext);
  const intent = context?.intent;
  if (!intent || intent.target.id !== id) return null;
  return (
    <div
      data-drop-edge={intent.edge}
      data-drop-valid={intent.valid}
      className={cn(
        "pointer-events-none absolute inset-x-0 z-10",
        intent.edge === "after" ? "bottom-0" : "top-0",
        intent.valid ? "border-t-4 border-primary" : "border-t-4 border-destructive",
        intent.edge === "empty" && "inset-0 rounded-md border-2 border-dashed",
      )}
    >
      <span className="absolute left-1 top-0 max-w-full break-words rounded bg-background px-1 text-xs text-foreground shadow">
        {intent.message}
      </span>
    </div>
  );
}

export function ChoiceDraggable({
  id,
  data,
  label,
  children,
  className,
  disabled = false,
}: {
  id: string;
  data: ChoiceDragData;
  label: string;
  children: ReactNode;
  className?: string;
  disabled?: boolean;
}) {
  const context = useContext(DragContext);
  const { setNodeRef, setActivatorNodeRef, attributes, listeners, isDragging } = useSortable({
    id,
    data,
    disabled,
  });
  const item = { id, data, label };
  return (
    <div
      ref={(node) => {
        setNodeRef(node);
        context?.register(item, disabled ? null : node, false);
      }}
      className={cn(
        "relative flex min-w-0 items-start gap-1 rounded-md",
        (isDragging || context?.active === id) && "opacity-40",
        className,
      )}
    >
      <DropCue id={id} />
      {!disabled && (
        <button
          ref={setActivatorNodeRef}
          type="button"
          aria-label={`Drag ${label}`}
          {...attributes}
          {...listeners}
          data-choice-handle
          aria-describedby={context?.instructions}
          onKeyDown={(event) => context?.keyboard(event, item)}
          className="grid size-9 shrink-0 cursor-grab select-none place-items-center rounded text-muted-foreground [-webkit-touch-callout:none] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          <GripVerticalIcon className="size-4" aria-hidden="true" />
        </button>
      )}
      {children}
    </div>
  );
}

export function ChoiceDropZone({
  id,
  data,
  label,
  children,
  className,
}: {
  id: string;
  data: ChoiceDragData;
  label: string;
  children: ReactNode;
  className?: string;
}) {
  const context = useContext(DragContext);
  const { setNodeRef } = useDroppable({ id, data });
  return (
    <div
      ref={(node) => {
        setNodeRef(node);
        context?.register({ id, data, label }, node, true);
      }}
      aria-label={label}
      className={cn("relative min-w-0 rounded-lg border border-border p-2", className)}
    >
      <DropCue id={id} />
      {children}
    </div>
  );
}
