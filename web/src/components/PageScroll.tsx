import { type CSSProperties, type ReactNode, useLayoutEffect, useRef, useState } from "react";

import { cn } from "@/lib/utils";

/**
 * The single scroll primitive for routed pages. It owns the one thing every
 * page used to reinvent by hand: keeping content clear of the chrome at the top
 * (the AppShell header + OS safe area) and bottom (the iOS native chat/terminal
 * bar + OS safe area).
 *
 * All of that comes from the shared inset variables defined in index.css
 * (`--omnigent-header-height`, `--omnigent-inset-top/bottom`). Off the iOS shell
 * those resolve to `0` / plain `env(safe-area-*)`, so the SAME component renders
 * correctly in the browser, Electron, and the iOS shell with no runtime branch.
 *
 * This is the "respect the inset" side of the system. Full-bleed elements that
 * intentionally ignore the inset (the webview, app-shell background, sidebar
 * drawer) simply don't use this — they stay edge-to-edge as before.
 */
interface PageScrollProps {
  children: ReactNode;
  /** Reserve room for the AppShell's absolute header at the top. Default true. */
  clearHeader?: boolean;
  /** Reserve the top OS safe-area inset. Default true. */
  insetTop?: boolean;
  /** Reserve the bottom inset (safe area + native chat/terminal bar). Default true. */
  insetBottom?: boolean;
  /** Extra top padding beyond the header/inset, as a CSS length. */
  extraTop?: string;
  /** Extra bottom padding beyond the inset, as a CSS length. */
  extraBottom?: string;
  /** Max-width class for the centered content column. Default "max-w-3xl". */
  maxWidthClassName?: string;
  /** Extra classes for the centered content column (e.g. horizontal padding). */
  contentClassName?: string;
  /** Extra classes for the scroll container itself. */
  className?: string;
  /** Forwarded to the scroll container (e.g. data-testid). */
  "data-testid"?: string;
}

export function PageScroll({
  children,
  clearHeader = true,
  insetTop = true,
  insetBottom = true,
  extraTop = "1.5rem",
  extraBottom = "2rem",
  maxWidthClassName = "max-w-3xl",
  contentClassName,
  className,
  "data-testid": testId,
}: PageScrollProps) {
  const viewportRef = useRef<HTMLDivElement>(null);
  const columnRef = useRef<HTMLDivElement>(null);
  const [navigationOverlapsContent, setNavigationOverlapsContent] = useState(false);

  // Content and header controls can change without a viewport breakpoint change.
  useLayoutEffect(() => {
    const viewport = viewportRef.current;
    const column = columnRef.current;
    if (!viewport || !column) return;
    const header = viewport.closest("main")?.parentElement?.querySelector(".chat-header");
    let observedToggle: HTMLElement | null = null;
    const measure = () => {
      if (viewport.getBoundingClientRect().width === 0) return;
      const toggle = header?.querySelector<HTMLElement>(".chat-header-sidebar-toggle") ?? null;
      if (toggle !== observedToggle) {
        if (observedToggle) resize.unobserve(observedToggle);
        if (toggle) resize.observe(toggle);
        observedToggle = toggle;
      }
      const toggleRect = toggle?.getBoundingClientRect();
      if (
        !toggle ||
        !toggleRect?.width ||
        !toggleRect.height ||
        getComputedStyle(toggle).visibility === "hidden"
      ) {
        setNavigationOverlapsContent(false);
        return;
      }
      // A single wrapper may supply the page's leading padding, as Usage does.
      let content: HTMLElement = column;
      while (
        content.childElementCount === 1 &&
        !Array.from(content.childNodes).some(
          (node) => node.nodeType === Node.TEXT_NODE && node.textContent?.trim(),
        )
      ) {
        const child = content.firstElementChild;
        if (!(child instanceof HTMLElement)) break;
        const position = getComputedStyle(child).position;
        if (position === "absolute" || position === "fixed") break;
        content = child;
      }
      const contentRect = content.getBoundingClientRect();
      const contentStyle = getComputedStyle(content);
      const left = contentRect.left + parseFloat(contentStyle.paddingLeft);
      const right = contentRect.right - parseFloat(contentStyle.paddingRight);
      setNavigationOverlapsContent(left < toggleRect.right && right > toggleRect.left);
    };
    const resize = new ResizeObserver(measure);
    resize.observe(viewport);
    resize.observe(column);
    if (header) resize.observe(header);
    const mutations = new MutationObserver(measure);
    if (header) mutations.observe(header, { attributes: true, childList: true, subtree: true });
    measure();
    return () => {
      resize.disconnect();
      mutations.disconnect();
    };
  });

  // Reserve clearance whenever content can meet the floating control. Separated
  // desktop columns retain their full-height viewport and scrollable padding.
  const style = {
    "--page-scroll-top-clearance": `calc(0px${clearHeader ? " + var(--omnigent-header-height)" : ""}${
      insetTop ? " + var(--omnigent-inset-top)" : ""
    })`,
    "--page-scroll-extra-top": extraTop,
    paddingBottom: `calc(${extraBottom}${insetBottom ? " + var(--omnigent-inset-bottom)" : ""})`,
  } as CSSProperties;

  return (
    <div
      ref={viewportRef}
      data-testid={testId}
      data-navigation-clearance={navigationOverlapsContent || undefined}
      className={cn(
        "min-h-0 w-full flex-1 overflow-y-auto mt-[var(--page-scroll-top-clearance)] pt-[var(--page-scroll-extra-top)] md:mt-0 md:pt-[calc(var(--page-scroll-top-clearance)_+_var(--page-scroll-extra-top))] data-[navigation-clearance]:mt-[var(--page-scroll-top-clearance)] data-[navigation-clearance]:pt-[var(--page-scroll-extra-top)]",
        className,
      )}
      style={style}
    >
      <div ref={columnRef} className={cn("mx-auto w-full", maxWidthClassName, contentClassName)}>
        {children}
      </div>
    </div>
  );
}
