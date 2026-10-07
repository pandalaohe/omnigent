import { StrictMode, createRef } from "react";
import { act, render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { assignLabels } from "@/lib/composerTokens";
import { ComposerTokenBackdrop, hasTint } from "./ComposerTokenBackdrop";

/** Captures rAF callbacks so a test can flush exactly one frame, the way the
 *  browser would after the synchronous measure cap trips. */
function installManualRaf() {
  const pending = new Map<number, FrameRequestCallback>();
  let nextId = 1;
  vi.stubGlobal("requestAnimationFrame", (cb: FrameRequestCallback) => {
    const id = nextId++;
    pending.set(id, cb);
    return id;
  });
  vi.stubGlobal("cancelAnimationFrame", (id: number) => {
    pending.delete(id);
  });
  return {
    flush() {
      const callbacks = [...pending.values()];
      pending.clear();
      act(() => {
        callbacks.forEach((cb) => cb(0));
      });
    },
  };
}

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
  afterEach(() => {
    vi.unstubAllGlobals();
  });

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

  it("N4 — caps synchronous re-measures when the anchor's box oscillates between reads", () => {
    const anchor = createRef<HTMLTextAreaElement>();
    const widths = [300, 340];
    let reads = 0;
    const oscillatingRef = (el: HTMLTextAreaElement | null) => {
      anchor.current = el;
      if (!el) return;
      Object.defineProperty(el, "offsetWidth", {
        configurable: true,
        get: () => widths[reads++ % widths.length],
      });
    };
    const ui = (value: string) => (
      <>
        <textarea ref={oscillatingRef} />
        <ComposerTokenBackdrop
          value={value}
          files={[]}
          command={null}
          activeIndex={null}
          anchor={anchor}
        />
      </>
    );
    const { container, rerender } = render(ui("hello"));
    expect(() => rerender(ui("hello [file 1]"))).not.toThrow();
    const node = container.querySelector(
      '[data-testid="composer-highlight-overlay"]',
    ) as HTMLElement;
    expect(["300px", "340px"]).toContain(node.style.width);
  });

  it("R1 — retries a capped measure after a position-only anchor move", () => {
    const raf = installManualRaf();
    const anchor = createRef<HTMLTextAreaElement>();
    const ui = (value: string) => (
      <>
        <textarea ref={anchor} />
        <ComposerTokenBackdrop
          value={value}
          files={[]}
          command={null}
          activeIndex={null}
          anchor={anchor}
        />
      </>
    );
    const { container, rerender } = render(ui("a"));
    // Each rerender is one layout-effect measure; after the cap trips, the
    // measure is skipped until the pending frame flushes.
    for (const value of ["b", "c", "d", "e", "f"]) rerender(ui(value));
    // A position-only move (a reply quote above the anchor): ResizeObserver
    // never sees it, so only the frame's retry can pick it up.
    act(() => {
      Object.defineProperty(anchor.current!, "offsetLeft", { value: 42, configurable: true });
    });
    raf.flush();
    const node = container.querySelector(
      '[data-testid="composer-highlight-overlay"]',
    ) as HTMLElement;
    expect(node.style.left).toBe("42px");
  });

  it("R2 — under StrictMode, frames still lift the cap so a later move re-aligns", () => {
    const raf = installManualRaf();
    const anchor = createRef<HTMLTextAreaElement>();
    const ui = (value: string) => (
      <StrictMode>
        <textarea ref={anchor} />
        <ComposerTokenBackdrop
          value={value}
          files={[]}
          command={null}
          activeIndex={null}
          anchor={anchor}
        />
      </StrictMode>
    );
    const { container, rerender } = render(ui("a"));
    // StrictMode replays the mount effects: its simulated unmount cancels the
    // frame and resets the cap, so later renders must still schedule frames.
    for (const value of ["b", "c", "d", "e", "f"]) rerender(ui(value));
    raf.flush();
    act(() => {
      Object.defineProperty(anchor.current!, "offsetLeft", { value: 64, configurable: true });
    });
    rerender(ui("g"));
    const node = container.querySelector(
      '[data-testid="composer-highlight-overlay"]',
    ) as HTMLElement;
    expect(node.style.left).toBe("64px");
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
