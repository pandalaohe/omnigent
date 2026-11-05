import type { ReactNode } from "react";
import { useDroppable } from "@dnd-kit/core";
import { useSortable } from "@dnd-kit/sortable";
import { GripVerticalIcon } from "lucide-react";
import { cn } from "@/lib/utils";

export type ChoiceDragData =
  | { kind: "preset"; preset: string }
  | { kind: "row"; tier: number; row: number }
  | { kind: "work"; work: string }
  | { kind: "tier"; tier: number }
  | { kind: "ability"; level: number; preset: string }
  | { kind: "ability-level"; level: number };

export function ChoiceDraggable({
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
  const { setNodeRef, setActivatorNodeRef, attributes, listeners, isDragging } = useSortable({
    id,
    data,
  });
  return (
    <div
      ref={setNodeRef}
      className={cn(
        "flex min-w-0 items-start gap-1 rounded-md",
        isDragging && "opacity-40",
        className,
      )}
    >
      <button
        ref={setActivatorNodeRef}
        type="button"
        aria-label={`Drag ${label}`}
        {...attributes}
        {...listeners}
        className="grid size-9 shrink-0 cursor-grab select-none place-items-center rounded text-muted-foreground [-webkit-touch-callout:none] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring md:size-7"
      >
        <GripVerticalIcon className="size-4" aria-hidden="true" />
      </button>
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
  const { setNodeRef, isOver } = useDroppable({ id, data });
  return (
    <div
      ref={setNodeRef}
      aria-label={label}
      className={cn(
        "min-w-0 rounded-lg border border-border p-2",
        isOver && "border-primary bg-primary/5",
        className,
      )}
    >
      {children}
    </div>
  );
}
