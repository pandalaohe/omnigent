import { act, cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { WorkspaceTabsScroller, workspaceTabLayout } from "./WorkspaceTabsScroller";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
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
