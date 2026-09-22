import { createRef } from "react";
import { act, render } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { assignLabels } from "@/lib/composerTokens";
import { ComposerTokenBackdrop, hasTint } from "./ComposerTokenBackdrop";

function img(name = "a.png"): File {
  return new File([new Uint8Array(1)], name, { type: "image/png" });
}

function renderBackdrop(props: Partial<React.ComponentProps<typeof ComposerTokenBackdrop>> = {}) {
  const anchor = createRef<HTMLTextAreaElement>();
  const { container } = render(
    <>
      <textarea ref={anchor} />
      <ComposerTokenBackdrop
        value={props.value ?? ""}
        files={props.files ?? []}
        command={props.command ?? null}
        activeIndex={props.activeIndex ?? null}
        anchor={anchor}
      />
    </>,
  );
  return container.querySelector('[data-testid="composer-highlight-overlay"]') as HTMLElement;
}

describe("hasTint", () => {
  it("is false for plain prose", () => {
    expect(hasTint("hello there", [])).toBe(false);
  });

  it("is true when a live token is present", () => {
    const file = img();
    assignLabels([file], []);
    expect(hasTint("see [image 1]", [file])).toBe(true);
  });

  it("is false for a token whose file isn't live", () => {
    expect(hasTint("see [image 9]", [])).toBe(false);
  });

  it("is true for an S26 reference", () => {
    expect(hasTint("look at ⟦Omnigent reference | abc123⟧", [])).toBe(true);
  });
});

describe("ComposerTokenBackdrop", () => {
  it("tints a leading command prefix", () => {
    const node = renderBackdrop({ value: "/review please", command: "/review" });
    const span = node.querySelector(".text-brand-accent");
    expect(span?.textContent).toBe("/review");
  });

  it("tints a live token and strengthens the active one", () => {
    const i1 = img("a.png");
    const i2 = img("b.png");
    assignLabels([i1, i2], []);
    const node = renderBackdrop({
      value: "a [image 1] b [image 2]",
      files: [i1, i2],
      activeIndex: 1,
    });
    const tokenSpans = [...node.querySelectorAll(".text-primary")];
    expect(tokenSpans.map((el) => el.textContent)).toEqual(["[image 1]", "[image 2]"]);
    expect(tokenSpans[1].className).toContain("bg-primary/15");
    expect(tokenSpans[0].className).not.toContain("bg-primary/15");
  });

  it("tints an S26 reference in colour/background only, no layout classes", () => {
    const node = renderBackdrop({ value: "see ⟦Omnigent reference | xyz⟧ now" });
    const span = node.querySelector(".bg-primary\\/10");
    expect(span?.textContent).toBe("⟦Omnigent reference | xyz⟧");
    // F1: padding, border width, font family, and font size would misalign
    // this text against the native caret — only colour/background/radius.
    expect(span?.className).not.toMatch(/\bpx-|\bpy-|\bborder\b|font-mono|text-\[/);
  });

  it("F4 — a live token inside an S26 reference is not rendered twice", () => {
    const file = img();
    assignLabels([file], []);
    const node = renderBackdrop({
      value: "see ⟦Omnigent reference | [image 1]⟧ now",
      files: [file],
    });
    expect(node.textContent).toBe("see ⟦Omnigent reference | [image 1]⟧ now");
  });

  it("does not tint an orphan token that has no live file", () => {
    const node = renderBackdrop({ value: "see [image 9]", files: [] });
    expect(node.querySelector(".text-primary")).toBeNull();
    expect(node.textContent).toBe("see [image 9]");
  });

  it("renders a trailing newline as one <br>, no extra text node", () => {
    const node = renderBackdrop({ value: "hello\n" });
    expect(node.querySelectorAll("br")).toHaveLength(1);
    expect(node.textContent).toBe("hello");
  });

  it("textContent equals the value exactly when it has no trailing newline", () => {
    const node = renderBackdrop({ value: "hello world" });
    expect(node.querySelectorAll("br")).toHaveLength(0);
    expect(node.textContent).toBe("hello world");
  });

  it("F1 — takes p-0, not the parent's padding (the textarea itself is p-0)", () => {
    const node = renderBackdrop({ value: "hello" });
    expect(node.className).toContain("p-0");
    expect(node.className).not.toMatch(/\bpx-3\b|\bpt-3\b|\bpb-1\b/);
  });

  it("F1 — re-measures the anchor's position on every render, not only on resize", () => {
    const anchor = createRef<HTMLTextAreaElement>();
    const { rerender, container } = render(
      <>
        <textarea ref={anchor} />
        <ComposerTokenBackdrop
          value="a"
          files={[]}
          command={null}
          activeIndex={null}
          anchor={anchor}
        />
      </>,
    );
    const node = () =>
      container.querySelector('[data-testid="composer-highlight-overlay"]') as HTMLElement;
    expect(node().style.top).toBe("0px");
    // The anchor moved (a reply quote was inserted above it) without any
    // resize — jsdom never fires ResizeObserver for this, so only a
    // render-scoped remeasure can pick it up.
    act(() => {
      Object.defineProperty(anchor.current!, "offsetTop", { value: 120, configurable: true });
    });
    rerender(
      <>
        <textarea ref={anchor} />
        <ComposerTokenBackdrop
          value="b"
          files={[]}
          command={null}
          activeIndex={null}
          anchor={anchor}
        />
      </>,
    );
    expect(node().style.top).toBe("120px");
  });

  it("N3 — measures on mount even when rendered before its anchor (ChatComposer's real order)", () => {
    const anchor = createRef<HTMLTextAreaElement>();
    const stubGeometry = (el: HTMLTextAreaElement | null) => {
      anchor.current = el;
      if (!el) return;
      Object.defineProperty(el, "offsetTop", { value: 20, configurable: true });
      Object.defineProperty(el, "offsetLeft", { value: 10, configurable: true });
      Object.defineProperty(el, "offsetWidth", { value: 300, configurable: true });
      Object.defineProperty(el, "offsetHeight", { value: 60, configurable: true });
    };
    const { container } = render(
      <>
        <ComposerTokenBackdrop
          value="hello"
          files={[]}
          command={null}
          activeIndex={null}
          anchor={anchor}
        />
        <textarea ref={stubGeometry} />
      </>,
    );
    const node = container.querySelector(
      '[data-testid="composer-highlight-overlay"]',
    ) as HTMLElement;
    expect(node.style.top).toBe("20px");
    expect(node.style.left).toBe("10px");
    expect(node.style.width).toBe("300px");
    expect(node.style.height).toBe("60px");
  });
});
