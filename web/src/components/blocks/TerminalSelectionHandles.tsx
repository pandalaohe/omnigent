import { useLayoutEffect, useRef } from "react";
import type { SelectionHandle, SelectionHandles } from "@/lib/terminalTouchSelection";

interface HandleEvents {
  onStart: (handle: SelectionHandle, event: TouchEvent) => (() => void) | undefined;
}

function Handle({
  handle,
  point,
  onStart,
}: HandleEvents & {
  handle: SelectionHandle;
  point: NonNullable<SelectionHandles[SelectionHandle]>;
}) {
  const cancelDrag = useRef<(() => void) | undefined>(undefined);
  useLayoutEffect(() => () => cancelDrag.current?.(), []);
  return (
    <div
      data-testid={`terminal-selection-${handle}`}
      className="pointer-events-auto absolute grid size-6 -translate-x-1/2 -translate-y-1/2 touch-none cursor-grab place-items-center"
      style={{ left: point.x, top: point.y }}
      onTouchStart={(event) => {
        const cancel = onStart(handle, event.nativeEvent);
        if (cancel) cancelDrag.current = cancel;
      }}
    >
      <span className="size-3.5 rounded-full border-2 border-background bg-primary shadow-sm" />
    </div>
  );
}

export function TerminalSelectionHandles({
  positions,
  onStart,
}: HandleEvents & { positions: SelectionHandles }) {
  return (
    <div className="pointer-events-none absolute inset-3 z-10" aria-hidden="true">
      {(["start", "end"] as const).map((handle) => {
        const point = positions[handle];
        return point && <Handle key={handle} handle={handle} point={point} onStart={onStart} />;
      })}
    </div>
  );
}
