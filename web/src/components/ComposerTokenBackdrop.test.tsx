import { StrictMode, useRef } from "react";
import { act, render } from "@testing-library/react";
import { describe, expect, it, onTestFinished, vi } from "vitest";

import { useComposerMirrorAlignment } from "@/components/composer/MentionTokenOverlay";
import { assignLabels } from "@/lib/composerTokens";
import { ComposerTokenBackdrop, hasTint } from "./ComposerTokenBackdrop";

function img(name = "a.png"): File {
  return new File([new Uint8Array(1)], name, { type: "image/png" });
}

function renderBackdrop(props: Partial<React.ComponentProps<typeof ComposerTokenBackdrop>> = {}) {
  const { container } = render(
    <ComposerTokenBackdrop
      value={props.value ?? ""}
      files={props.files ?? []}
      command={props.command ?? null}
      activeIndex={props.activeIndex ?? null}
      mentionTokens={props.mentionTokens}
    />,
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
  it("paints session mentions and upload tokens together without changing the text", () => {
    const image = img();
    assignLabels([image], []);
    const value = "ask @Reviewer about [image 1]";
    const node = renderBackdrop({ value, files: [image], mentionTokens: ["@Reviewer"] });
    expect(node.textContent).toBe(value);
    expect(node.querySelector('[data-testid="composer-mention-token"]')?.textContent).toBe(
      "@Reviewer",
    );
    expect(node.querySelector(".text-primary")?.textContent).toBe("[image 1]");
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

  it("keeps the blank line after a trailing newline in the backdrop's scroll range", () => {
    const node = renderBackdrop({ value: "hello\n" });
    expect(node.querySelectorAll("br")).toHaveLength(1);
    expect(node.textContent).toBe("hello\u00a0");
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

  it("aligns on oscillating anchor reads without triggering a React update loop", () => {
    const notifyResize = new Set<() => void>();
    vi.stubGlobal(
      "ResizeObserver",
      class {
        private notify: () => void;
        constructor(callback: ResizeObserverCallback) {
          this.notify = () => callback([], this as unknown as ResizeObserver);
        }
        observe() {
          notifyResize.add(this.notify);
        }
        unobserve() {}
        disconnect() {
          notifyResize.delete(this.notify);
        }
      },
    );
    onTestFinished(() => {
      vi.unstubAllGlobals();
    });

    const widths = [300, 340];
    let reads = 0;
    let top = 20;

    function AlignedBackdrop({ value }: { value: string }) {
      const overlayRef = useRef<HTMLDivElement>(null);
      const textareaRef = useRef<HTMLTextAreaElement>(null);
      useComposerMirrorAlignment(overlayRef, textareaRef, true);
      const attachAnchor = (el: HTMLTextAreaElement | null) => {
        textareaRef.current = el;
        if (!el) return;
        Object.defineProperties(el, {
          offsetTop: { configurable: true, get: () => top },
          offsetLeft: { configurable: true, value: 10 },
          offsetHeight: { configurable: true, value: 60 },
          offsetWidth: {
            configurable: true,
            get: () => widths[reads++ % widths.length],
          },
        });
      };
      // Mount backdrop before textarea, matching ChatComposer's DOM order.
      return (
        <>
          <ComposerTokenBackdrop
            ref={overlayRef}
            value={value}
            files={[]}
            command={null}
            activeIndex={null}
          />
          <textarea ref={attachAnchor} value={value} onChange={() => {}} />
        </>
      );
    }

    const { container, rerender } = render(
      <StrictMode>
        <AlignedBackdrop value="hello" />
      </StrictMode>,
    );
    const node = container.querySelector(
      '[data-testid="composer-highlight-overlay"]',
    ) as HTMLElement;
    expect(node.style.top).toBe("20px");
    expect(node.style.left).toBe("10px");
    expect(node.style.height).toBe("60px");
    expect(["300px", "340px"]).toContain(node.style.width);

    // Repeated rerenders and resize notifications with alternating widths must
    // never loop or drop a synchronous position update.
    expect(() => {
      for (const value of ["a", "b", "c", "d", "e", "hello [file 1]"]) {
        rerender(
          <StrictMode>
            <AlignedBackdrop value={value} />
          </StrictMode>,
        );
      }
    }).not.toThrow();
    top = 48;
    rerender(
      <StrictMode>
        <AlignedBackdrop value="moved [file 1]" />
      </StrictMode>,
    );
    expect(node.style.top).toBe("48px");
    expect(["300px", "340px"]).toContain(node.style.width);

    const beforeResizeWidth = node.style.width;
    act(() => notifyResize.forEach((notify) => notify()));
    expect(node.style.width).toBe(beforeResizeWidth === "300px" ? "340px" : "300px");
  });
});
