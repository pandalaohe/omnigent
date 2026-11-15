import { expect, it } from "vitest";
import { overlayAtPointer } from "./choiceOverlay";

it.each([0, 800])(
  "anchors the overlay at the viewport pointer after %s px page scroll",
  (scroll) => {
    const rect = {
      left: 400,
      top: 900 - scroll,
      right: 600,
      bottom: 950 - scroll,
      width: 200,
      height: 50,
    };
    const start = { x: 418, y: rect.top + 18 };
    const pointer = { x: 60, y: 420 };
    const transform = { x: pointer.x - start.x, y: pointer.y - start.y, scaleX: 1, scaleY: 1 };
    const moved = overlayAtPointer({
      activatorEvent: new MouseEvent("mousedown", { clientX: start.x, clientY: start.y }),
      active: null,
      activeNodeRect: rect,
      draggingNodeRect: rect,
      containerNodeRect: null,
      over: null,
      overlayNodeRect: rect,
      scrollableAncestors: [],
      scrollableAncestorRects: [],
      transform,
      windowRect: null,
    });
    expect(rect.left + moved.x).toBe(pointer.x + 12);
    expect(rect.top + moved.y).toBe(pointer.y + 12);
  },
);
