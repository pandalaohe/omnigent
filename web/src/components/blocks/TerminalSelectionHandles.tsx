import type { SelectionHandle, SelectionHandles } from "@/lib/terminalTouchSelection";

export function TerminalSelectionHandles({
  positions,
  onStart,
}: {
  positions: SelectionHandles;
  onStart: (handle: SelectionHandle, event: TouchEvent) => void;
}) {
  return (
    <div className="pointer-events-none absolute inset-3 z-10" aria-hidden="true">
      {(["start", "end"] as const).map((handle) => {
        const point = positions[handle];
        if (!point) return null;
        return (
          <div
            key={handle}
            data-testid={`terminal-selection-${handle}`}
            className="pointer-events-auto absolute grid size-6 -translate-x-1/2 -translate-y-1/2 touch-none cursor-grab place-items-center"
            style={{ left: point.x, top: point.y }}
            onTouchStart={(event) => onStart(handle, event.nativeEvent)}
          >
            <span className="size-3.5 rounded-full border-2 border-background bg-primary shadow-sm" />
          </div>
        );
      })}
    </div>
  );
}
