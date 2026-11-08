import { useEffect, useRef, useState, type PointerEvent } from "react";
import {
  ArchiveIcon,
  ArrowDownIcon,
  ArrowLeftIcon,
  ArrowRightIcon,
  ArrowUpIcon,
  ChevronsDownIcon,
  ChevronsUpIcon,
  CommandIcon,
  CornerDownLeftIcon,
  DeleteIcon,
  GripVerticalIcon,
  KeyboardIcon,
  LockKeyholeIcon,
  RefreshCwIcon,
  SquareTerminalIcon,
  XIcon,
  type LucideIcon,
} from "lucide-react";

import { useIsPhone } from "@/hooks/useIsPhone";
import {
  MOBILE_ASSISTANT_CHANGED_EVENT,
  MOBILE_ASSISTANT_MAX_BUTTONS,
  dispatchMobileAssistantButton,
  dispatchTerminalBarKey,
  bindTerminalBarTarget,
  flattenBar,
  mobileAssistantBindingSupportsRepeat,
  readMobileAssistantPreferences,
  resolveTerminalBar,
  useTypableTerminals,
  visibleChatButtons,
  writeMobileAssistantDeviceState,
  type MobileAssistantButton,
  type MobileAssistantDock,
  type MobileAssistantDockEdge,
  type MobileAssistantIcon,
  type TerminalBarKey,
  type TerminalBarTarget,
  type TerminalStickyModifiers,
} from "@/lib/mobileAssistantPreferences";
import { cn } from "@/lib/utils";

const MAIN_SIZE = 52;
const ACTION_SIZE = 46;
const ACTION_GAP = 8;
const ACTION_EDGE_MARGIN = 6;
const HANDLE_THICKNESS = 18;
const HANDLE_LENGTH = 48;
const DOCK_THRESHOLD = 38;
// On portrait phones this clears the composer band (input, action row, queued
// messages), keyboard-shortened viewport included; a stored or docked spot wins.
const FIRST_USE_POINT = { x: 0.86, y: 0.2 };
type WheelItem = MobileAssistantButton | TerminalBarKey;

function repeats(item: WheelItem): boolean {
  if ("binding" in item)
    return item.repeat === true && mobileAssistantBindingSupportsRepeat(item.binding);
  return item.kind === "key" && item.repeat === true;
}

interface ViewportSize {
  width: number;
  height: number;
}

interface Point {
  x: number;
  y: number;
}

function viewportSize(): ViewportSize {
  return {
    width: Math.max(window.visualViewport?.width ?? window.innerWidth, 1),
    height: Math.max(window.visualViewport?.height ?? window.innerHeight, 1),
  };
}

function clamp(value: number, minimum: number, maximum: number): number {
  return Math.min(Math.max(value, minimum), Math.max(minimum, maximum));
}

export interface MobileAssistantCircleLayout {
  center: Point;
  radius: number;
  actions: Point[];
  actionSize: number;
  twoRings: boolean;
  innerCount: number;
}

function smoothstep(value: number): number {
  const normalized = clamp(value, 0, 1);
  return normalized * normalized * (3 - 2 * normalized);
}

/**
 * Keep the configured order visible from the top. The center stays where the
 * user placed it: a full circle is used in open space, while an edge/corner
 * folds the same ordered buttons into two inward-facing arcs.
 */
export function layoutMobileAssistantCircle(
  count: number,
  requestedCenter: Point,
  viewport: ViewportSize,
): MobileAssistantCircleLayout {
  const center = {
    x: clamp(requestedCenter.x, MAIN_SIZE / 2, viewport.width - MAIN_SIZE / 2),
    y: clamp(requestedCenter.y, MAIN_SIZE / 2, viewport.height - MAIN_SIZE / 2),
  };
  const fullChord = ACTION_SIZE + ACTION_GAP;
  const fullNeededRadius = count > 1 ? fullChord / (2 * Math.sin(Math.PI / Math.max(count, 2))) : 0;
  const fullRadius = Math.max(82, Math.min(210, fullNeededRadius));
  const layoutThreshold = fullRadius + ACTION_SIZE / 2 + ACTION_EDGE_MARGIN;
  const pressureRange = layoutThreshold - MAIN_SIZE / 2;
  const left = smoothstep((layoutThreshold - center.x) / pressureRange);
  const right = smoothstep((layoutThreshold - (viewport.width - center.x)) / pressureRange);
  const top = smoothstep((layoutThreshold - center.y) / pressureRange);
  const bottom = smoothstep((layoutThreshold - (viewport.height - center.y)) / pressureRange);
  const horizontalPressure = Math.max(left, right);
  const verticalPressure = Math.max(top, bottom);
  const edgePressure = Math.max(horizontalPressure, verticalPressure);
  const cornerPressure = Math.min(horizontalPressure, verticalPressure);
  const compression = clamp(edgePressure + cornerPressure * 0.55, 0, 1);
  const twoRings = edgePressure > 0 && count > 0;
  const actionSize = Math.max(44, ACTION_SIZE - compression * 3);
  const gap = ACTION_GAP - compression * 4;
  const chord = actionSize + gap;
  const innerCount = twoRings ? Math.ceil(count / 2) : count;
  const outerCount = twoRings ? count - innerCount : 0;
  const largestRing = Math.max(innerCount, outerCount, 1);
  let constrainHorizontal = twoRings && horizontalPressure > 0;
  let constrainVertical = twoRings && verticalPressure > 0;
  const spanForConstraints = (): number =>
    twoRings ? (constrainHorizontal && constrainVertical ? Math.PI / 2 : Math.PI) : Math.PI * 2;
  const radiiForSpan = (arcSpan: number): { radius: number; outerRadius: number } => {
    const divisor = twoRings ? Math.max(largestRing - 1, 1) : Math.max(count, 1);
    const step = arcSpan / divisor;
    const neededRadius = largestRing > 1 ? chord / (2 * Math.sin(Math.max(step, 0.05) / 2)) : 0;
    const radius = Math.max(twoRings ? 66 : fullRadius, Math.min(210, neededRadius));
    return { radius, outerRadius: radius + chord * 0.96 };
  };

  let span = spanForConstraints();
  let { radius, outerRadius } = radiiForSpan(span);
  if (twoRings) {
    const outerExtent = outerRadius + actionSize / 2 + ACTION_EDGE_MARGIN;
    constrainHorizontal ||= Math.min(center.x, viewport.width - center.x) < outerExtent;
    constrainVertical ||= Math.min(center.y, viewport.height - center.y) < outerExtent;
    span = spanForConstraints();
    ({ radius, outerRadius } = radiiForSpan(span));
  }

  const horizontalDirection = center.x <= viewport.width - center.x ? 1 : -1;
  const verticalDirection = center.y <= viewport.height - center.y ? 1 : -1;
  const inward = {
    x: constrainHorizontal ? horizontalDirection : 0,
    y: constrainVertical ? verticalDirection : 0,
  };
  const inwardAngle = Math.hypot(inward.x, inward.y) > 0.01 ? Math.atan2(inward.y, inward.x) : 0;
  const start = twoRings ? inwardAngle - span / 2 : -Math.PI / 2;
  const end = start + span;
  const startVector = { x: Math.cos(start), y: Math.sin(start) };
  const endVector = { x: Math.cos(end), y: Math.sin(end) };
  const reverseArc =
    twoRings &&
    (startVector.y > endVector.y + 0.001 ||
      (Math.abs(startVector.y - endVector.y) <= 0.001 && startVector.x > endVector.x));
  const orderedStart = reverseArc ? end : start;
  const direction = reverseArc ? -1 : 1;
  const ringPositions = (ringCount: number, ringRadius: number): Point[] => {
    if (ringCount === 0) return [];
    const ringStep = twoRings ? span / Math.max(ringCount - 1, 1) : span / ringCount;
    return Array.from({ length: ringCount }, (_, slot) => {
      const angle = orderedStart + direction * ringStep * slot;
      return {
        x: center.x + Math.cos(angle) * ringRadius,
        y: center.y + Math.sin(angle) * ringRadius,
      };
    });
  };
  const rawActions = twoRings
    ? [...ringPositions(innerCount, radius), ...ringPositions(outerCount, outerRadius)]
    : ringPositions(count, radius);
  const inset = actionSize / 2;
  const minX = Math.min(...rawActions.map((point) => point.x));
  const maxX = Math.max(...rawActions.map((point) => point.x));
  const minY = Math.min(...rawActions.map((point) => point.y));
  const maxY = Math.max(...rawActions.map((point) => point.y));
  const translateAxis = (minimum: number, maximum: number, limit: number): number => {
    const lower = inset - minimum;
    const upper = limit - inset - maximum;
    return lower <= upper ? clamp(0, lower, upper) : (lower + upper) / 2;
  };
  const translateX = rawActions.length > 0 ? translateAxis(minX, maxX, viewport.width) : 0;
  const translateY = rawActions.length > 0 ? translateAxis(minY, maxY, viewport.height) : 0;
  const actions = rawActions.map((point) => ({
    x: point.x + translateX,
    y: point.y + translateY,
  }));
  return { center, radius, actions, actionSize, twoRings, innerCount };
}

export function layoutMobileAssistantActions(
  count: number,
  center: Point,
  viewport: ViewportSize,
): Point[] {
  return layoutMobileAssistantCircle(count, center, viewport).actions;
}

function nearestDock(
  clientX: number,
  clientY: number,
  viewport: ViewportSize,
): MobileAssistantDock | undefined {
  const distances: { edge: MobileAssistantDockEdge; distance: number }[] = [
    { edge: "left", distance: clientX },
    { edge: "right", distance: viewport.width - clientX },
    { edge: "top", distance: clientY },
    { edge: "bottom", distance: viewport.height - clientY },
  ];
  const nearest = distances.sort((a, b) => a.distance - b.distance)[0];
  if (!nearest || nearest.distance > DOCK_THRESHOLD) return undefined;
  const horizontal = nearest.edge === "top" || nearest.edge === "bottom";
  return {
    edge: nearest.edge,
    offset: clamp(horizontal ? clientX / viewport.width : clientY / viewport.height, 0, 1),
  };
}

function dockPoint(dock: MobileAssistantDock, viewport: ViewportSize): Point {
  const edgeMargin = HANDLE_LENGTH / 2 + 8;
  if (dock.edge === "left" || dock.edge === "right") {
    return {
      x: dock.edge === "left" ? HANDLE_THICKNESS / 2 : viewport.width - HANDLE_THICKNESS / 2,
      y: clamp(dock.offset * viewport.height, edgeMargin, viewport.height - edgeMargin),
    };
  }
  return {
    x: clamp(dock.offset * viewport.width, edgeMargin, viewport.width - edgeMargin),
    y: dock.edge === "top" ? HANDLE_THICKNESS / 2 : viewport.height - HANDLE_THICKNESS / 2,
  };
}

function restoredPoint(dock: MobileAssistantDock, viewport: ViewportSize): Point {
  const handle = dockPoint(dock, viewport);
  if (dock.edge === "left") return { x: MAIN_SIZE / 2 + 12, y: handle.y };
  if (dock.edge === "right") return { x: viewport.width - MAIN_SIZE / 2 - 12, y: handle.y };
  if (dock.edge === "top") return { x: handle.x, y: MAIN_SIZE / 2 + 12 };
  return { x: handle.x, y: viewport.height - MAIN_SIZE / 2 - 12 };
}

const MOBILE_ASSISTANT_ICON_COMPONENTS: Record<MobileAssistantIcon, LucideIcon> = {
  escape: XIcon,
  tab: ArrowRightIcon,
  "arrow-up": ArrowUpIcon,
  "arrow-down": ArrowDownIcon,
  "arrow-left": ArrowLeftIcon,
  "arrow-right": ArrowRightIcon,
  enter: CornerDownLeftIcon,
  backspace: DeleteIcon,
  delete: DeleteIcon,
  "page-up": ChevronsUpIcon,
  "page-down": ChevronsDownIcon,
  keyboard: KeyboardIcon,
  refresh: RefreshCwIcon,
  archive: ArchiveIcon,
  terminal: SquareTerminalIcon,
  command: CommandIcon,
};

export function MobileAssistantButtonContent({
  button,
  className = "h-4 w-4",
}: {
  button: Pick<MobileAssistantButton, "label" | "icon" | "display"> &
    Partial<Pick<TerminalBarKey, "icon">>;
  className?: string;
}) {
  const Icon = button.icon
    ? MOBILE_ASSISTANT_ICON_COMPONENTS[button.icon as MobileAssistantIcon]
    : undefined;
  if (button.display === "icon" && Icon) return <Icon className={className} aria-hidden />;
  return (
    <span
      className={cn("max-w-full", Array.from(button.label).length === 1 ? "shrink-0" : "truncate")}
    >
      {button.label}
    </span>
  );
}

export function MobileFloatingAssistant() {
  const isPhone = useIsPhone();
  const terminals = useTypableTerminals();
  const { count, withBar } = terminals;
  const [preferences, setPreferences] = useState(readMobileAssistantPreferences);
  const [open, setOpen] = useState(false);
  const [sticky, setSticky] = useState<TerminalStickyModifiers>({
    ctrl: "off",
    alt: "off",
    shift: "off",
  });
  const stickySubscription = useRef<(() => void) | null>(null);
  const [viewport, setViewport] = useState<ViewportSize>(() => viewportSize());
  const [dragPreview, setDragPreview] = useState<Point | null>(null);
  const dragRef = useRef<{
    pointerId: number;
    startX: number;
    startY: number;
    moved: boolean;
  } | null>(null);
  const suppressClickRef = useRef(false);
  const suppressActionClickRef = useRef<string | null>(null);
  const repeatTimeoutRef = useRef<number | null>(null);
  const repeatIntervalRef = useRef<number | null>(null);
  const repeatingPointerRef = useRef<{
    buttonId: string;
    pointerId: number;
    x: number;
    y: number;
  } | null>(null);
  const assistantRef = useRef<HTMLDivElement>(null);
  const lastInputFocusRef = useRef<HTMLElement | null>(
    typeof document !== "undefined" &&
      document.activeElement instanceof HTMLElement &&
      (document.activeElement instanceof HTMLInputElement ||
        document.activeElement instanceof HTMLTextAreaElement ||
        document.activeElement.isContentEditable)
      ? document.activeElement
      : null,
  );

  useEffect(() => {
    const refresh = () => setPreferences(readMobileAssistantPreferences());
    window.addEventListener(MOBILE_ASSISTANT_CHANGED_EVENT, refresh);
    window.addEventListener("storage", refresh);
    return () => {
      window.removeEventListener(MOBILE_ASSISTANT_CHANGED_EVENT, refresh);
      window.removeEventListener("storage", refresh);
    };
  }, []);

  useEffect(() => {
    setSticky({ ctrl: "off", alt: "off", shift: "off" });
    return () => {
      stickySubscription.current?.();
      stickySubscription.current = null;
    };
  }, [terminals]);

  const stopRepeating = () => {
    if (repeatTimeoutRef.current !== null) window.clearTimeout(repeatTimeoutRef.current);
    if (repeatIntervalRef.current !== null) window.clearInterval(repeatIntervalRef.current);
    repeatTimeoutRef.current = null;
    repeatIntervalRef.current = null;
    repeatingPointerRef.current = null;
  };

  const cancelActionPress = () => {
    if (repeatingPointerRef.current)
      suppressActionClickRef.current = repeatingPointerRef.current.buttonId;
    stopRepeating();
  };

  useEffect(() => {
    const onVisibility = () => {
      if (document.visibilityState === "hidden") cancelActionPress();
    };
    window.addEventListener("blur", cancelActionPress);
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      window.removeEventListener("blur", cancelActionPress);
      document.removeEventListener("visibilitychange", onVisibility);
      cancelActionPress();
    };
  }, []);

  useEffect(() => {
    if (!open || !preferences.enabled || withBar > 0) cancelActionPress();
  }, [open, preferences.enabled, withBar]);

  useEffect(() => cancelActionPress(), [terminals]);

  useEffect(() => {
    const rememberFocus = (event: FocusEvent) => {
      const target = event.target;
      // Closing menus can focus app chrome; retain the user's input destination.
      if (
        target instanceof HTMLElement &&
        (target instanceof HTMLInputElement ||
          target instanceof HTMLTextAreaElement ||
          target.isContentEditable) &&
        !assistantRef.current?.contains(target)
      ) {
        lastInputFocusRef.current = target;
      }
    };
    document.addEventListener("focusin", rememberFocus);
    return () => document.removeEventListener("focusin", rememberFocus);
  }, []);

  useEffect(() => {
    const resize = () => setViewport(viewportSize());
    window.addEventListener("resize", resize);
    window.visualViewport?.addEventListener("resize", resize);
    return () => {
      window.removeEventListener("resize", resize);
      window.visualViewport?.removeEventListener("resize", resize);
    };
  }, []);

  const point = preferences.position ?? FIRST_USE_POINT;
  const center = {
    x: clamp(point.x * viewport.width, MAIN_SIZE / 2, viewport.width - MAIN_SIZE / 2),
    y: clamp(point.y * viewport.height, MAIN_SIZE / 2, viewport.height - MAIN_SIZE / 2),
  };
  const bar = resolveTerminalBar(preferences as unknown as Record<string, unknown>).bar;
  const buttons: WheelItem[] =
    count > 0
      ? flattenBar(bar).slice(0, MOBILE_ASSISTANT_MAX_BUTTONS)
      : visibleChatButtons(preferences).slice(0, MOBILE_ASSISTANT_MAX_BUTTONS);
  const circle = layoutMobileAssistantCircle(buttons.length, center, viewport);
  const displayCenter = open ? circle.center : center;
  const actionPositions = circle.actions;

  const fireButton = (button: WheelItem, gesture: "tap" | "longPress" | "repeat" = "tap") => {
    if ("binding" in button) dispatchMobileAssistantButton(button, lastInputFocusRef.current);
    else {
      stickySubscription.current?.();
      stickySubscription.current = null;
      dispatchTerminalBarKey(
        button,
        lastInputFocusRef.current,
        {
          onStickyChanged: setSticky,
          onStickySubscribed: (unsubscribe) => {
            stickySubscription.current = unsubscribe;
          },
        },
        gesture,
      );
    }
  };

  const onActionPointerDown = (button: WheelItem, event: PointerEvent<HTMLButtonElement>) => {
    if (event.button === 0) event.preventDefault();
    if (event.button !== 0 || repeatingPointerRef.current) return;
    suppressActionClickRef.current = null;
    const modifier = !("binding" in button) && button.kind === "modifier";
    if (!repeats(button) && !modifier) return;
    stopRepeating();
    let target: TerminalBarTarget | null = null;
    if (!("binding" in button)) {
      stickySubscription.current?.();
      target = bindTerminalBarTarget(lastInputFocusRef.current, {
        onStickyChanged: setSticky,
        onStickySubscribed: (unsubscribe) => {
          stickySubscription.current = unsubscribe;
        },
      });
      if (!target) return;
    }
    repeatingPointerRef.current = {
      buttonId: button.id,
      pointerId: event.pointerId,
      x: event.clientX,
      y: event.clientY,
    };
    event.currentTarget.setPointerCapture?.(event.pointerId);
    if ("binding" in button) {
      suppressActionClickRef.current = button.id;
      fireButton(button);
    }
    repeatTimeoutRef.current = window.setTimeout(
      () => {
        if (target && !target.isCurrent()) {
          cancelActionPress();
          return;
        }
        suppressActionClickRef.current = button.id;
        if (target && !("binding" in button))
          target.press(button, modifier ? "longPress" : "repeat");
        else fireButton(button, "repeat");
        if (!modifier)
          repeatIntervalRef.current = window.setInterval(() => {
            if (target && !target.isCurrent()) {
              cancelActionPress();
              return;
            }
            if (target && !("binding" in button)) target.press(button, "repeat");
            else fireButton(button, "repeat");
          }, 80);
      },
      modifier ? 500 : 400,
    );
  };

  const onActionPointerEnd = (button: WheelItem, pointerId: number) => {
    const repeating = repeatingPointerRef.current;
    if (!repeating || repeating.buttonId !== button.id || repeating.pointerId !== pointerId) return;
    stopRepeating();
    window.setTimeout(() => {
      if (suppressActionClickRef.current === button.id) suppressActionClickRef.current = null;
    }, 0);
  };

  const normalizedPoint = (clientX: number, clientY: number) => ({
    x: clamp(clientX, MAIN_SIZE / 2, viewport.width - MAIN_SIZE / 2) / viewport.width,
    y: clamp(clientY, MAIN_SIZE / 2, viewport.height - MAIN_SIZE / 2) / viewport.height,
  });

  const onPointerDown = (event: PointerEvent<HTMLButtonElement>) => {
    if (event.button === 0) event.preventDefault();
    dragRef.current = {
      pointerId: event.pointerId,
      startX: event.clientX,
      startY: event.clientY,
      moved: false,
    };
    event.currentTarget.setPointerCapture?.(event.pointerId);
  };

  const onPointerMove = (event: PointerEvent<HTMLButtonElement>) => {
    const drag = dragRef.current;
    if (!drag || drag.pointerId !== event.pointerId) return;
    if (Math.hypot(event.clientX - drag.startX, event.clientY - drag.startY) > 5) {
      drag.moved = true;
      setOpen(false);
    }
    if (drag.moved) {
      setDragPreview({ x: event.clientX, y: event.clientY });
    }
  };

  const onPointerUp = (event: PointerEvent<HTMLButtonElement>) => {
    const drag = dragRef.current;
    if (!drag || drag.pointerId !== event.pointerId) return;
    dragRef.current = null;
    setDragPreview(null);
    suppressClickRef.current = drag.moved;
    if (!drag.moved) return;
    const dock = nearestDock(event.clientX, event.clientY, viewport);
    const next = {
      ...preferences,
      position: normalizedPoint(event.clientX, event.clientY),
      dock,
    };
    setPreferences(next);
    writeMobileAssistantDeviceState(next);
  };

  const undock = () => {
    if (!preferences.dock) return;
    const restored = restoredPoint(preferences.dock, viewport);
    const next = {
      ...preferences,
      dock: undefined,
      position: { x: restored.x / viewport.width, y: restored.y / viewport.height },
    };
    setPreferences(next);
    writeMobileAssistantDeviceState(next);
  };

  if (!isPhone || !preferences.enabled || buttons.length === 0 || withBar > 0) {
    return null;
  }

  const commonPointerProps = {
    onPointerDown,
    onPointerMove,
    onPointerUp,
  };

  return (
    <div
      ref={assistantRef}
      className="pointer-events-none fixed inset-0 z-40"
      data-testid="mobile-floating-assistant"
    >
      {preferences.dock ? (
        <button
          type="button"
          aria-label="Expand floating assistant"
          data-dock-edge={preferences.dock.edge}
          title="Drag away from the edge to restore"
          {...commonPointerProps}
          onClick={() => {
            if (suppressClickRef.current) {
              suppressClickRef.current = false;
              return;
            }
            undock();
          }}
          className={cn(
            "phone-hit-area pointer-events-auto fixed flex touch-none select-none items-center justify-center border border-primary/30 bg-primary text-primary-foreground shadow-lg",
            "focus-visible:ring-2 focus-visible:ring-ring active:brightness-110",
            preferences.dock.edge === "left" || preferences.dock.edge === "right"
              ? "h-12 w-[18px]"
              : "h-[18px] w-12",
            preferences.dock.edge === "left" && "rounded-r-lg border-l-0",
            preferences.dock.edge === "right" && "rounded-l-lg border-r-0",
            preferences.dock.edge === "top" && "rounded-b-lg border-t-0",
            preferences.dock.edge === "bottom" && "rounded-t-lg border-b-0",
          )}
          style={{
            left: (dragPreview ?? dockPoint(preferences.dock, viewport)).x,
            top: (dragPreview ?? dockPoint(preferences.dock, viewport)).y,
            transform: "translate(-50%, -50%)",
          }}
        >
          <GripVerticalIcon
            className={cn(
              "h-4 w-4",
              (preferences.dock.edge === "top" || preferences.dock.edge === "bottom") &&
                "rotate-90",
            )}
          />
        </button>
      ) : (
        <>
          {open
            ? buttons.map((button, index) => {
                const position = actionPositions[index] ?? center;
                return (
                  <button
                    key={button.id}
                    type="button"
                    aria-label={button.label}
                    title={button.label}
                    data-display={
                      "display" in button && button.display === "icon" && button.icon
                        ? "icon"
                        : "text"
                    }
                    aria-pressed={
                      "kind" in button && button.kind === "modifier"
                        ? sticky[button.modifier] !== "off"
                        : undefined
                    }
                    data-sticky-state={
                      "kind" in button && button.kind === "modifier"
                        ? sticky[button.modifier]
                        : undefined
                    }
                    data-repeat={"repeat" in button && button.repeat === true ? "true" : undefined}
                    onPointerDown={(event) => onActionPointerDown(button, event)}
                    onPointerMove={(event) => {
                      const press = repeatingPointerRef.current;
                      if (
                        press?.pointerId === event.pointerId &&
                        Math.hypot(event.clientX - press.x, event.clientY - press.y) > 10
                      ) {
                        suppressActionClickRef.current = button.id;
                        stopRepeating();
                      }
                    }}
                    onPointerUp={(event) => onActionPointerEnd(button, event.pointerId)}
                    onPointerCancel={(event) => {
                      if (repeatingPointerRef.current?.pointerId === event.pointerId)
                        cancelActionPress();
                    }}
                    onLostPointerCapture={(event) => {
                      if (repeatingPointerRef.current?.pointerId === event.pointerId)
                        cancelActionPress();
                    }}
                    onClick={(event) => {
                      if (event.detail !== 0 && suppressActionClickRef.current === button.id) {
                        suppressActionClickRef.current = null;
                        return;
                      }
                      suppressActionClickRef.current = null;
                      fireButton(button);
                    }}
                    className={cn(
                      "pointer-events-auto fixed flex items-center justify-center rounded-full border border-border bg-popover px-1 text-sm font-semibold text-popover-foreground shadow-lg",
                      "touch-manipulation select-none active:scale-95",
                      "kind" in button &&
                        button.kind === "modifier" &&
                        sticky[button.modifier] !== "off" &&
                        "border-primary-cta bg-primary-cta text-primary-cta-foreground",
                    )}
                    style={{
                      left: position.x,
                      top: position.y,
                      width: circle.actionSize,
                      height: circle.actionSize,
                      transform: "translate(-50%, -50%)",
                    }}
                  >
                    <MobileAssistantButtonContent button={button} />
                    {"kind" in button &&
                      button.kind === "modifier" &&
                      sticky[button.modifier] === "locked" && (
                        <LockKeyholeIcon className="ml-0.5 size-3" aria-hidden />
                      )}
                  </button>
                );
              })
            : null}
          <button
            type="button"
            aria-label={open ? "Close floating assistant" : "Open floating assistant"}
            aria-expanded={open}
            {...commonPointerProps}
            onClick={() => {
              if (suppressClickRef.current) {
                suppressClickRef.current = false;
                return;
              }
              setOpen((current) => !current);
            }}
            className="pointer-events-auto fixed flex h-[52px] w-[52px] touch-none select-none items-center justify-center rounded-full border border-primary/30 bg-primary text-primary-foreground shadow-xl active:scale-95"
            style={{
              left: (dragPreview ?? displayCenter).x,
              top: (dragPreview ?? displayCenter).y,
              transform: "translate(-50%, -50%)",
            }}
          >
            <KeyboardIcon className="h-5 w-5" />
          </button>
        </>
      )}
    </div>
  );
}
