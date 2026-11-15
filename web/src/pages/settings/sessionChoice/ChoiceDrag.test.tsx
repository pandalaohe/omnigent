import type { ComponentProps } from "react";
import type * as Core from "@dnd-kit/core";
import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import type { DndContext, DragMoveEvent, DragStartEvent, DragEndEvent } from "@dnd-kit/core";
import { ChoiceDragContext, ChoiceDraggable } from "./ChoiceDrag";

type ContextProps = ComponentProps<typeof DndContext>;
const drag = vi.hoisted(() => ({
  current: undefined as ContextProps | undefined,
  overlay: undefined as ComponentProps<typeof Core.DragOverlay> | undefined,
}));
vi.mock("@dnd-kit/core", async (original) => ({
  ...(await original<typeof Core>()),
  DndContext: (props: ContextProps) => {
    drag.current = props;
    return props.children;
  },
  pointerWithin: () => [{ id: "target" }],
  DragOverlay: (props: ComponentProps<typeof Core.DragOverlay>) => {
    drag.overlay = props;
    return <div>{props.children}</div>;
  },
}));
vi.mock("@dnd-kit/sortable", () => ({
  useSortable: () => ({
    setNodeRef: () => {},
    setActivatorNodeRef: () => {},
    attributes: {},
    listeners: {},
    isDragging: false,
  }),
}));
afterEach(cleanup);

it("keeps the overlay outside scrolled or transformed list ancestors", () => {
  const { container } = render(
    <div style={{ transform: "translateY(300px)", overflow: "auto", height: 100 }}>
      <ChoiceDragContext reset="draft" onDrop={() => ({ focus: "source", message: "Moved" })}>
        <ChoiceDraggable id="source" data={{ kind: "work", work: "work" }} label="Source">
          Source
        </ChoiceDraggable>
      </ChoiceDragContext>
    </div>,
  );
  act(() => drag.current?.onDragStart?.({ active: { id: "source" } } as DragStartEvent));
  const overlay = screen.getByText("Move Source");
  expect(document.body).toContainElement(overlay);
  expect(container).not.toContainElement(overlay);
});

it("retains the pickup origin when the source rectangle shifts during a drag", () => {
  render(
    <ChoiceDragContext reset="draft" onDrop={() => ({ focus: "source", message: "Moved" })}>
      <ChoiceDraggable id="source" data={{ kind: "work", work: "work" }} label="Source">
        Source
      </ChoiceDraggable>
    </ChoiceDragContext>,
  );
  const handle = screen.getByRole("button", { name: "Drag Source" });
  const origin = new DOMRect(400, 200, 200, 50);
  const bounds = vi.spyOn(handle.parentElement!, "getBoundingClientRect").mockReturnValue(origin);
  act(() => drag.current?.onDragStart?.({ active: { id: "source" } } as DragStartEvent));
  bounds.mockReturnValue(new DOMRect(400, -100, 200, 50));
  const result = drag.overlay!.modifiers![0]({
    activatorEvent: new MouseEvent("mousedown", { clientX: 418, clientY: 218 }),
    active: null,
    activeNodeRect: bounds(),
    draggingNodeRect: null,
    containerNodeRect: null,
    over: null,
    overlayNodeRect: null,
    scrollableAncestors: [],
    scrollableAncestorRects: [],
    transform: { x: -358, y: 202, scaleX: 1, scaleY: 1 },
    windowRect: null,
  });
  expect(origin.left + result.x).toBe(72);
  expect(origin.top + result.y).toBe(432);
  bounds.mockRestore();
});

it("uses the collision pointer position after scrolling, rather than scroll-adjusted movement", () => {
  const drop = vi.fn(() => ({ focus: "source", message: "Moved" }));
  const active = {
    id: "source",
    data: { current: {} },
    rect: { current: { initial: null, translated: null } },
  };
  const { container } = render(
    <ChoiceDragContext reset="draft" onDrop={drop}>
      <ChoiceDraggable id="source" data={{ kind: "row", tier: 0, row: 0 }} label="Source">
        Source
      </ChoiceDraggable>
      <ChoiceDraggable id="target" data={{ kind: "row", tier: 0, row: 1 }} label="Target">
        Target
      </ChoiceDraggable>
    </ChoiceDragContext>,
  );
  act(() => drag.current?.onDragStart?.({ active: { id: "source" } } as DragStartEvent));
  act(() => {
    drag.current?.collisionDetection?.({ pointerCoordinates: { x: 20, y: 110 } } as Parameters<
      NonNullable<ContextProps["collisionDetection"]>
    >[0]);
    drag.current?.onDragMove?.({
      active,
      over: {
        id: "target",
        rect: { top: 100, height: 100, bottom: 200, left: 0, right: 100, width: 100 },
        disabled: false,
        data: { current: {} },
      },
      activatorEvent: new MouseEvent("mousedown", { clientY: 10 }),
      delta: { x: 0, y: 190 },
      collisions: null,
    } satisfies DragMoveEvent);
  });
  expect(container.querySelector('[data-drop-edge="before"]')).toBeInTheDocument();
  act(() => drag.current?.onDragEnd?.({ active: { id: "source" } } as DragEndEvent));
  expect(drop).toHaveBeenCalledWith(expect.objectContaining({ edge: "before", valid: true }));
});
