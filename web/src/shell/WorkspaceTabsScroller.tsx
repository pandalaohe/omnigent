import { type ReactNode, useLayoutEffect, useRef } from "react";

/** Recompute visibility when a tab is selected, without moving any scroller. */
export function revealWorkspaceTab(tab: HTMLElement | null): void {
  tab?.closest("[data-workspace-tabs-viewport]")?.dispatchEvent(new Event("workspace-tab-reveal"));
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
