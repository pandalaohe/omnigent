// The owner-UI composer that collects the note for a picked element: position
// (below the pick, flipped near the bottom, clamped inside the preview) and the
// keyboard contract (Enter send, Cmd/Ctrl+Enter stack, Shift+Enter newline,
// Escape cancel).

import { fireEvent, render, screen } from "@testing-library/react";
import { createRef } from "react";
import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from "vitest";
import { AnnotationComposer, type AnnotationComposerProps } from "./AnnotationComposer";

function rect(x: number, y: number, w: number, h: number): DOMRect {
  return {
    x,
    y,
    left: x,
    top: y,
    width: w,
    height: h,
    right: x + w,
    bottom: y + h,
    toJSON: () => ({}),
  } as DOMRect;
}

const PREVIEW_RECT = rect(0, 0, 800, 600);
const IFRAME_RECT = rect(100, 50, 600, 400);
const COMPOSER_RECT = rect(0, 0, 320, 120);

let rectSpy: MockInstance;

beforeEach(() => {
  rectSpy = vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (
    this: HTMLElement,
  ) {
    if (this.getAttribute("data-testid") === "preview") return PREVIEW_RECT;
    if (this.tagName === "IFRAME") return IFRAME_RECT;
    if (this.getAttribute("data-testid") === "annotation-composer") return COMPOSER_RECT;
    return rect(0, 0, 0, 0);
  });
});

afterEach(() => {
  rectSpy.mockRestore();
});

function renderComposer(overrides: Partial<AnnotationComposerProps> = {}) {
  const previewRef = createRef<HTMLDivElement>();
  const onSubmit = vi.fn();
  const onCancel = vi.fn();
  render(
    <div ref={previewRef} data-testid="preview">
      <AnnotationComposer
        label="div.filter-menu > button.option"
        viewportRect={{ x: 120, y: 80, w: 100, h: 30 }}
        previewRef={previewRef}
        iframe={document.createElement("iframe")}
        onSubmit={onSubmit}
        onCancel={onCancel}
        {...overrides}
      />
    </div>,
  );
  return {
    composer: screen.getByTestId("annotation-composer"),
    textarea: screen.getByPlaceholderText("What should change?"),
    onSubmit,
    onCancel,
  };
}

describe("AnnotationComposer position", () => {
  it("sits below the picked rect with the 8 px gap in preview coordinates", () => {
    const { composer } = renderComposer();

    expect(composer.style.left).toBe("220px");
    expect(composer.style.top).toBe("168px");
    expect(composer.style.visibility).toBe("");
  });

  it("flips above the target when below would overflow the preview", () => {
    const { composer } = renderComposer({
      viewportRect: { x: 120, y: 400, w: 100, h: 30 },
    });

    // 450 top + 30 h + 8 gap + 120 box = 608 > 600 - 8: flip to 450 - 120 - 8.
    expect(composer.style.top).toBe("322px");
  });

  it("clamps the box inside the preview horizontally", () => {
    const { composer } = renderComposer({
      viewportRect: { x: 650, y: 80, w: 100, h: 30 },
    });

    // The pick's left maps to 750; the box clamps to 800 - 320 - 8.
    expect(composer.style.left).toBe("472px");
  });
});

describe("AnnotationComposer content and keys", () => {
  it("shows the full label on a truncated mono line and autofocuses the textarea", () => {
    const { composer, textarea } = renderComposer();

    const label = composer.querySelector("[title]")!;
    expect(label.textContent).toBe("div.filter-menu > button.option");
    expect(label.getAttribute("title")).toBe("div.filter-menu > button.option");
    expect(label.className).toContain("truncate");
    expect(label.className).toContain("font-mono");
    expect(textarea).toHaveFocus();
  });

  it("sends on Enter, stacks on Cmd/Ctrl+Enter, keeps Shift+Enter a newline", () => {
    const { textarea, onSubmit } = renderComposer();
    fireEvent.change(textarea, { target: { value: "needs work" } });

    fireEvent.keyDown(textarea, { key: "Enter", shiftKey: true });
    expect(onSubmit).not.toHaveBeenCalled();

    fireEvent.keyDown(textarea, { key: "Enter", isComposing: true });
    expect(onSubmit).not.toHaveBeenCalled();

    fireEvent.keyDown(textarea, { key: "Enter" });
    expect(onSubmit).toHaveBeenCalledWith("needs work", "send");

    fireEvent.keyDown(textarea, { key: "Enter", metaKey: true });
    expect(onSubmit).toHaveBeenLastCalledWith("needs work", "stack");

    fireEvent.keyDown(textarea, { key: "Enter", ctrlKey: true });
    expect(onSubmit).toHaveBeenLastCalledWith("needs work", "stack");
    expect(onSubmit).toHaveBeenCalledTimes(3);
  });

  it("cancels on Escape without submitting", () => {
    const { textarea, onSubmit, onCancel } = renderComposer();

    fireEvent.keyDown(textarea, { key: "Escape" });

    expect(onCancel).toHaveBeenCalledTimes(1);
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("submits through the Stack and Send buttons", () => {
    const { textarea, onSubmit } = renderComposer();
    fireEvent.change(textarea, { target: { value: "via button" } });

    fireEvent.click(screen.getByRole("button", { name: "Stack ⌘↵" }));
    expect(onSubmit).toHaveBeenLastCalledWith("via button", "stack");

    fireEvent.click(screen.getByRole("button", { name: "Send ↵" }));
    expect(onSubmit).toHaveBeenLastCalledWith("via button", "send");
  });
});
