import { useLayoutEffect, useRef, useState, type FocusEvent, type ReactNode } from "react";

/**
 * Phone session bar row. Labels collapse first; when the controls still outgrow
 * the bar it scrolls sideways and fades the edge that hides more controls.
 * Keyboard focus scrolls the control clear of the faded edges.
 * Off phones it renders its children unchanged.
 */
export function PhoneBarScroller({ enabled, children }: { enabled: boolean; children: ReactNode }) {
  const scrollerRef = useRef<HTMLDivElement>(null);
  const rowRef = useRef<HTMLDivElement>(null);
  const [hidden, setHidden] = useState({ start: false, end: false });
  useLayoutEffect(() => {
    const scroller = scrollerRef.current;
    const row = rowRef.current;
    if (!enabled || !scroller || !row) return;
    const update = () => {
      const start = scroller.scrollLeft > 1;
      const end = scroller.scrollLeft + scroller.clientWidth < scroller.scrollWidth - 1;
      setHidden((current) =>
        current.start === start && current.end === end ? current : { start, end },
      );
    };
    update();
    scroller.addEventListener("scroll", update, { passive: true });
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(update);
    observer?.observe(scroller);
    observer?.observe(row);
    return () => {
      scroller.removeEventListener("scroll", update);
      observer?.disconnect();
    };
  }, [enabled]);
  if (!enabled) return children;
  return (
    <div
      ref={scrollerRef}
      data-testid="composer-workspace-scroller"
      data-workspace-scroller=""
      data-hidden-start={hidden.start || undefined}
      data-hidden-end={hidden.end || undefined}
      className="phone-session-bar-scroll min-w-0 flex-1 self-stretch"
      onFocus={revealKeyboardFocus}
    >
      <div ref={rowRef} className="flex h-full w-fit min-w-full items-center gap-1">
        {children}
      </div>
    </div>
  );
}

// Browsers leave a partly visible control where it is; scroll-padding keeps it out of the fade.
function revealKeyboardFocus(event: FocusEvent<HTMLDivElement>) {
  const target = event.target;
  if (target.matches(":focus-visible")) {
    target.scrollIntoView?.({ block: "nearest", inline: "nearest" });
  }
}
