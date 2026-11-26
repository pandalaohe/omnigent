import { act, cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  WorkspaceTabsScroller,
  workspaceTabLayout,
  scrollTabIntoView,
  scrollViewportToShow,
} from "./WorkspaceTabsScroller";

function rect(left: number, width: number): DOMRect {
  return { left, right: left + width, width, top: 0, bottom: 20, height: 20 } as DOMRect;
}

/** A 200px-wide tab strip at x=100 holding one 50px tab at `tabLeft`. */
function strip(tabLeft: number, tabWidth = 50) {
  const page = document.createElement("div");
  const viewport = document.createElement("div");
  viewport.setAttribute("data-workspace-tabs-viewport", "");
  const tab = document.createElement("div");
  viewport.append(tab);
  page.append(viewport);
  document.body.append(page);
  let scrollLeft = 40;
  Object.defineProperty(viewport, "scrollLeft", {
    get: () => scrollLeft,
    set: (value: number) => (scrollLeft = value),
  });
  Object.defineProperty(viewport, "clientWidth", { value: 200 });
  viewport.getBoundingClientRect = () => rect(100, 200);
  tab.getBoundingClientRect = () => rect(tabLeft, tabWidth);
  return { page, viewport, tab };
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("scrollTabIntoView", () => {
  // Without a visible width, showing a box uses the strip's whole width, as a tab does.
  it.each([
    ["a tab", (_viewport: HTMLElement, tab: HTMLElement) => scrollTabIntoView(tab)],
    [
      "a box",
      (viewport: HTMLElement, tab: HTMLElement) =>
        scrollViewportToShow(viewport, tab.getBoundingClientRect()),
    ],
  ])("scrolls only the strip to show %s past its right edge", (_shape, show) => {
    const { viewport, tab } = strip(280);
    const scrollIntoView = vi.spyOn(Element.prototype, "scrollIntoView");
    show(viewport, tab);
    expect(viewport.scrollLeft).toBe(70);
    expect(scrollIntoView).not.toHaveBeenCalled();
  });
});

describe("workspaceTabLayout", () => {
  it.each([2, 12])("fits %i tabs into a narrow strip with usable close controls", (count) => {
    const layout = workspaceTabLayout(252, count, count - 1);
    expect(layout.visible).toContain(count - 1);
    expect(layout.width).toBeGreaterThanOrEqual(64);
    expect(
      layout.width * layout.visible.length + (layout.visible.length - 1) * 2,
    ).toBeLessThanOrEqual(252);
    expect(layout.visible.length).toBe(count === 2 ? 2 : 3);
  });
  it("caps wide tabs and handles an empty strip", () => {
    expect(workspaceTabLayout(900, 2, 0).width).toBe(240);
    expect(workspaceTabLayout(0, 0, -1)).toEqual({ width: 0, visible: [] });
  });
  it("reveals a selected overflow tab and responds to removal without scrolling", async () => {
    vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockReturnValue(132);
    const tabs = (selected: number) =>
      Array.from({ length: 12 }, (_, index) => (
        <div key={index} data-workspace-tab aria-current={index === selected}>
          <button type="button">Close {index}</button>
        </div>
      ));
    const { container, rerender } = render(
      <WorkspaceTabsScroller>{tabs(0)}</WorkspaceTabsScroller>,
    );
    expect(container.querySelectorAll('[style*="display: flex"]')).toHaveLength(2);
    rerender(<WorkspaceTabsScroller>{tabs(11)}</WorkspaceTabsScroller>);
    await act(async () => {});
    expect(
      (container.querySelectorAll("[data-workspace-tab]")[11] as HTMLElement).style.display,
    ).toBe("flex");
    expect(container.firstElementChild?.scrollLeft).toBe(0);
    rerender(<WorkspaceTabsScroller>{tabs(11).slice(0, 1)}</WorkspaceTabsScroller>);
    await act(async () => {});
    expect(container.querySelectorAll('[style*="display: flex"]')).toHaveLength(1);
  });
});

describe("scrollViewportToShow", () => {
  it("shows a box within the strip's visible width, when part of the strip is covered", () => {
    const { viewport } = strip(0);
    // The box ends at x=280: inside the strip's 200px, but past its first 150px.
    scrollViewportToShow(viewport, rect(230, 50));
    expect(viewport.scrollLeft).toBe(40);
    scrollViewportToShow(viewport, rect(230, 50), 150);
    expect(viewport.scrollLeft).toBe(70);
  });
});
