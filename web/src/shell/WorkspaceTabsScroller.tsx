import { type ReactNode, useLayoutEffect, useRef } from "react";

/** Recompute visibility when a tab is selected, without moving any scroller. */
export function revealWorkspaceTab(tab: HTMLElement | null): void {
  tab?.closest("[data-workspace-tabs-viewport]")?.dispatchEvent(new Event("workspace-tab-reveal"));
}

/**
 * Scrolls the tab strip just far enough to show `tab`. Unlike `scrollIntoView`, it
 * moves no other scroller: the rail slides in from past the window's right edge,
 * so revealing a tab there would shift the whole page sideways.
 */
export function scrollTabIntoView(tab: HTMLElement | null): void {
  const viewport = tab?.closest<HTMLElement>("[data-workspace-tabs-viewport]");
  if (!tab || !viewport) return;
  scrollViewportToShow(viewport, tab.getBoundingClientRect());
}

/** Scroll `viewport` just far enough to show `box`, of which `width` pixels are visible. */
export function scrollViewportToShow(
  viewport: HTMLElement,
  box: DOMRect,
  width = viewport.clientWidth,
): void {
  const left = viewport.getBoundingClientRect().left + viewport.clientLeft;
  const right = left + width;
  if (box.left < left) {
    // A tab wider than the strip whose end already shows keeps its close button in view.
    if (box.right <= right && box.width > width) return;
    viewport.scrollLeft -= left - box.left;
  } else if (box.right > right) {
    // A tab wider than the strip keeps its start in view.
    viewport.scrollLeft += Math.min(box.right - right, box.left - left);
  }
}

/** Reserve 64px per visible tab for its icon, padding and close button. */
export function workspaceTabLayout(width: number, count: number, activeIndex: number) {
  const capacity = Math.max(1, Math.floor((width + 2) / 66));
  const visibleCount = Math.min(count, capacity);
  const visible = Array.from({ length: visibleCount }, (_, index) => index);
  if (activeIndex >= visibleCount && visibleCount > 0) visible[visibleCount - 1] = activeIndex;
  return {
    visible,
    width: visibleCount
      ? Math.max(0, Math.min(240, (width - (visibleCount - 1) * 2) / visibleCount))
      : 0,
  };
}

/** Fit tabs to the strip; excess tabs remain available in the panel picker. */
export function WorkspaceTabsScroller({ children }: { children: ReactNode }) {
  const viewportRef = useRef<HTMLDivElement>(null);

  useLayoutEffect(() => {
    const viewport = viewportRef.current;
    if (!viewport) return;
    const layout = () => {
      // Closed rails have no measurable slot until they become visible.
      if (viewport.clientWidth === 0) return;
      const tabs = Array.from(viewport.querySelectorAll<HTMLElement>("[data-workspace-tab]"));
      const activeIndex = tabs.findIndex(
        (tab) =>
          tab.matches('[aria-current="true"]') || !!tab.querySelector('[aria-selected="true"]'),
      );
      const result = workspaceTabLayout(viewport.clientWidth, tabs.length, activeIndex);
      tabs.forEach((tab, index) => {
        tab.style.display = result.visible.includes(index) ? "flex" : "none";
        tab.style.width = `${result.width}px`;
      });
    };
    const observer = new ResizeObserver(layout);
    const mutation = new MutationObserver(layout);
    observer.observe(viewport);
    mutation.observe(viewport, {
      childList: true,
      subtree: true,
      attributes: true,
      attributeFilter: ["aria-selected", "aria-current"],
    });
    viewport.addEventListener("workspace-tab-reveal", layout);
    layout();
    return () => {
      observer.disconnect();
      mutation.disconnect();
      viewport.removeEventListener("workspace-tab-reveal", layout);
    };
  }, []);

  return (
    <div
      ref={viewportRef}
      data-workspace-tabs-viewport
      className="no-drag flex min-w-0 flex-1 items-center gap-0.5 overflow-hidden"
    >
      {children}
    </div>
  );
}
