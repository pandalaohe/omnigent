import type { Modifier } from "@dnd-kit/core";

export const overlayAtPointer: Modifier = ({ activatorEvent, activeNodeRect, transform }) => {
  const point =
    activatorEvent instanceof MouseEvent
      ? activatorEvent
      : typeof TouchEvent !== "undefined" && activatorEvent instanceof TouchEvent
        ? activatorEvent.touches[0]
        : null;
  if (!point || !activeNodeRect) return transform;
  return {
    ...transform,
    x: transform.x + point.clientX - activeNodeRect.left + 12,
    y: transform.y + point.clientY - activeNodeRect.top + 12,
  };
};
