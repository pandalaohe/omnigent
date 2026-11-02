import type { CSSProperties, ReactNode } from "react";

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
  // Narrow layouts reserve header clearance outside the scroll viewport. Desktop
  // keeps its full-height scroll viewport and scrollable top padding.
  const style = {
    "--page-scroll-top-clearance": `calc(0px${clearHeader ? " + var(--omnigent-header-height)" : ""}${
      insetTop ? " + var(--omnigent-inset-top)" : ""
    })`,
    "--page-scroll-extra-top": extraTop,
    paddingBottom: `calc(${extraBottom}${insetBottom ? " + var(--omnigent-inset-bottom)" : ""})`,
  } as CSSProperties;

  return (
    <div
      data-testid={testId}
      className={cn(
        "min-h-0 w-full flex-1 overflow-y-auto mt-[var(--page-scroll-top-clearance)] pt-[var(--page-scroll-extra-top)] min-[1040px]:mt-0 min-[1040px]:pt-[calc(var(--page-scroll-top-clearance)_+_var(--page-scroll-extra-top))]",
        className,
      )}
      style={style}
    >
      <div className={cn("mx-auto w-full", maxWidthClassName, contentClassName)}>{children}</div>
    </div>
  );
}
