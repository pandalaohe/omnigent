// Owner-UI note box for element annotation (design delta K1): the frame only
// reports the pick, so the note is typed here and never in the page's realm.
// Rendered by HtmlCommentViewer while a pick is pending, over the preview.

import {
  type KeyboardEvent,
  type RefObject,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";

export interface AnnotationComposerProps {
  /** Full element/region label, shown on one truncated mono line. */
  label: string;
  /** The picked rect (or region box) in the frame's viewport CSS px. */
  viewportRect: { x: number; y: number; w: number; h: number };
  /** The preview container the composer is absolutely positioned inside. */
  previewRef: RefObject<HTMLDivElement | null>;
  /** The frame the picked rect belongs to, for its offset in the preview. */
  iframe: HTMLIFrameElement | null;
  onSubmit: (note: string, action: "stack" | "send") => void;
  onCancel: () => void;
}

const COMPOSER_GAP = 8;

/** Below the target, flipped above near the bottom, clamped inside the preview. */
export function annotationComposerPosition(input: {
  previewWidth: number;
  previewHeight: number;
  originX: number;
  originY: number;
  target: { x: number; y: number; w: number; h: number };
  composerWidth: number;
  composerHeight: number;
}): { left: number; top: number } {
  const targetLeft = input.originX + input.target.x;
  const targetTop = input.originY + input.target.y;
  const below = targetTop + input.target.h + COMPOSER_GAP;
  const flipped = below + input.composerHeight > input.previewHeight - COMPOSER_GAP;
  const top = flipped ? targetTop - input.composerHeight - COMPOSER_GAP : below;
  return {
    left: Math.max(
      COMPOSER_GAP,
      Math.min(targetLeft, input.previewWidth - input.composerWidth - COMPOSER_GAP),
    ),
    top: Math.max(
      COMPOSER_GAP,
      Math.min(top, input.previewHeight - input.composerHeight - COMPOSER_GAP),
    ),
  };
}

export function AnnotationComposer({
  label,
  viewportRect,
  previewRef,
  iframe,
  onSubmit,
  onCancel,
}: AnnotationComposerProps) {
  const composerRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const [note, setNote] = useState("");
  const [position, setPosition] = useState<{ left: number; top: number } | null>(null);

  const measure = useCallback(() => {
    const composer = composerRef.current;
    const preview = previewRef.current;
    if (!composer || !preview) return;
    const previewRect = preview.getBoundingClientRect();
    const iframeRect = iframe?.getBoundingClientRect() ?? previewRect;
    const composerRect = composer.getBoundingClientRect();
    setPosition(
      annotationComposerPosition({
        previewWidth: previewRect.width,
        previewHeight: previewRect.height,
        originX: iframeRect.left - previewRect.left,
        originY: iframeRect.top - previewRect.top,
        target: viewportRect,
        composerWidth: composerRect.width,
        composerHeight: composerRect.height,
      }),
    );
  }, [iframe, previewRef, viewportRect]);

  // The box stays hidden until measured; the effect (not layout) because the
  // preview container's ref is attached after this child's layout pass.
  useEffect(() => {
    measure();
    window.addEventListener("resize", measure);
    return () => window.removeEventListener("resize", measure);
  }, [measure]);

  // Focus only once the box is visible: a visibility:hidden element is not
  // focusable. The guard keeps a later resize from stealing focus back.
  const focusedRef = useRef(false);
  useEffect(() => {
    if (!position || focusedRef.current) return;
    focusedRef.current = true;
    textareaRef.current?.focus();
  }, [position]);

  const handleKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === "Escape") {
      event.preventDefault();
      onCancel();
      return;
    }
    if (event.key !== "Enter" || event.nativeEvent.isComposing || event.shiftKey) return;
    event.preventDefault();
    onSubmit(note, event.metaKey || event.ctrlKey ? "stack" : "send");
  };

  return (
    <div
      ref={composerRef}
      data-testid="annotation-composer"
      className="absolute z-50 flex w-80 flex-col gap-2 rounded-lg border border-border bg-popover p-3 text-sm text-foreground shadow-md backdrop-blur-xl backdrop-saturate-150"
      style={{
        left: position?.left ?? 0,
        top: position?.top ?? 0,
        visibility: position ? undefined : "hidden",
      }}
    >
      <div className="truncate font-mono text-xs text-muted-foreground" title={label}>
        {label}
      </div>
      <textarea
        ref={textareaRef}
        rows={3}
        placeholder="What should change?"
        value={note}
        onChange={(event) => setNote(event.target.value)}
        onKeyDown={handleKeyDown}
        className="w-full resize-none rounded-md border border-border bg-transparent px-2 py-1.5 text-sm text-foreground outline-none placeholder:text-muted-foreground focus:border-ring"
      />
      <div className="flex justify-end gap-2">
        <button
          type="button"
          className="rounded-md border border-border bg-popover px-2.5 py-1 text-sm font-medium text-foreground shadow-md transition-colors hover:bg-secondary"
          onClick={() => onSubmit(note, "stack")}
        >
          Stack ⌘↵
        </button>
        <button
          type="button"
          className="rounded-md border border-border bg-secondary px-2.5 py-1 text-sm font-medium text-foreground shadow-md transition-colors hover:bg-secondary/80"
          onClick={() => onSubmit(note, "send")}
        >
          Send ↵
        </button>
      </div>
    </div>
  );
}
