import { useEffect, useRef, useState, type ReactNode } from "react";
import { CheckIcon, Loader2Icon, MessagesSquareIcon, TerminalIcon } from "lucide-react";
import { useShortcutHint } from "@/components/KeyboardShortcut";
import { Segmented } from "@/components/ui/segmented";
import { DropdownMenuItem, DropdownMenuSeparator } from "@/components/ui/dropdown-menu";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { useIsMobileViewport } from "@/hooks/useIsMobileViewport";
import { useViewModeToggleHotkey } from "@/hooks/useViewModeToggleHotkey";
import { useOmnigentAnalytics } from "@/lib/analytics";
import { useTerminalFirst, type TerminalFirstView } from "./TerminalFirstContext";

const ignoreViewChange = () => {};

/**
 * Header Chat/Terminal switcher for terminal-first sessions. Two icon
 * segments in a shared track show both destinations at once, so the
 * active view is readable at a glance and switching is one click — no
 * menu to open. Status lives in the sidebar; this is purely a view
 * toggle.
 *
 * Self-gates to null when there's nothing to toggle:
 *   - non-terminal-first sessions,
 *   - a rail-opened shell owning the main view (isShellView) — its own
 *     close affordance is the way back to chat.
 *
 * On a desktop-width viewport it renders in the header on every shell — the
 * native iOS bottom pill is retired (AppShell pushes it hidden at boot), so the
 * header is the one placement. On a mobile-width viewport the segmented track
 * is dropped and the switch folds into the header kebab instead (see
 * {@link ViewModeMenuItems}), so the narrow header pill isn't split between a
 * view switch and the "…" menu.
 */
export function ViewModeToggle({ hotkeyEnabled = true, alwaysVisible = false } = {}) {
  const ctx = useTerminalFirst();
  const isMobile = useIsMobileViewport();
  const segmentedRef = useRef<HTMLDivElement>(null);
  const { trackClick } = useOmnigentAnalytics();
  const shortcutHint = useShortcutHint("toggleViewMode");
  const shortcutAria = shortcutHint.aria;
  const [openView, setOpenView] = useState<TerminalFirstView | null>(null);
  const toggleVisible = Boolean(
    ctx?.isTerminalFirst && !ctx.isShellView && (!isMobile || alwaysVisible),
  );
  useEffect(() => {
    if (!toggleVisible) setOpenView(null);
    const radios = segmentedRef.current?.querySelectorAll<HTMLElement>('[role="radio"]');
    radios?.forEach((radio) => {
      if (shortcutAria) radio.setAttribute("aria-keyshortcuts", shortcutAria);
      else radio.removeAttribute("aria-keyshortcuts");
    });
  }, [shortcutAria, toggleVisible]);
  useViewModeToggleHotkey({
    enabled: Boolean(hotkeyEnabled && ctx?.isTerminalFirst && !ctx.isShellView),
    view: ctx?.view ?? "chat",
    setView: ctx?.setView ?? ignoreViewChange,
  });
  if (!ctx || !ctx.isTerminalFirst || ctx.isShellView) return null;
  // Mobile folds the switch into the header kebab (ViewModeMenuItems).
  if (isMobile && !alwaysVisible) return null;

  const { view, setView, terminalStartingUp } = ctx;
  const terminalLabel = terminalStartingUp ? "Terminal is starting up…" : "Terminal view";

  return (
    <div
      ref={segmentedRef}
      data-testid="view-mode-toggle"
      className="h-full max-h-10 shrink-0"
      onFocusCapture={(event) => {
        const testId = (event.target as HTMLElement).dataset.testid;
        if (testId === "view-mode-chat") setOpenView("chat");
        if (testId === "view-mode-terminal") setOpenView("terminal");
      }}
      onBlurCapture={(event) => {
        if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setOpenView(null);
      }}
    >
      <Segmented
        label="Switch between chat and terminal"
        fitHeight
        options={[
          {
            value: "chat",
            label: (
              <ViewModeOptionLabel
                label="Chat view"
                shortcutHint={shortcutHint}
                open={openView === "chat"}
                onOpenChange={(open) => setOpenView(open ? "chat" : null)}
              >
                <MessagesSquareIcon className="size-4" aria-hidden />
              </ViewModeOptionLabel>
            ),
            title: "Chat view",
            testId: "view-mode-chat",
          },
          {
            value: "terminal",
            label: (
              <ViewModeOptionLabel
                label={terminalLabel}
                shortcutHint={shortcutHint}
                open={openView === "terminal"}
                onOpenChange={(open) => setOpenView(open ? "terminal" : null)}
              >
                {terminalStartingUp ? (
                  <Loader2Icon className="size-4 animate-spin" aria-hidden />
                ) : (
                  <TerminalIcon className="size-4" aria-hidden />
                )}
              </ViewModeOptionLabel>
            ),
            title: terminalLabel,
            testId: "view-mode-terminal",
          },
        ]}
        value={view}
        onChange={(next) => {
          trackClick(
            next === "chat" ? "chat.header.view_chat" : "chat.header.view_terminal",
            "button",
          );
          setView(next);
        }}
      />
    </div>
  );
}

/** Icon content for one radio; the existing styled hotkey tooltip stays with the segment. */
function ViewModeOptionLabel({
  label,
  shortcutHint,
  open,
  onOpenChange,
  children,
}: {
  label: string;
  shortcutHint: { keys: string[]; aria: string | undefined };
  open: boolean;
  onOpenChange: (open: boolean) => void;
  children: ReactNode;
}) {
  return (
    <Tooltip open={open} onOpenChange={onOpenChange}>
      <TooltipTrigger asChild>
        <span className="inline-flex items-center justify-center">
          {children}
          <span className="sr-only">{label}</span>
        </span>
      </TooltipTrigger>
      {/* Bottom placement keeps the header tooltip inside the viewport. */}
      <TooltipContent side="bottom" shortcut={shortcutHint.keys}>
        <span>{label}</span>
      </TooltipContent>
    </Tooltip>
  );
}

/**
 * Chat/Terminal switch as dropdown-menu items — the mobile counterpart of the
 * {@link ViewModeToggle} segmented track, folded into the header kebab so the
 * narrow header pill carries one trigger instead of a switch beside the "…"
 * menu. A shell keeps both destinations reachable; only non-terminal-first
 * sessions hide these entries. The active view
 * carries a trailing check; a trailing separator sets the switch off from the
 * menu items that follow.
 */
export function ViewModeMenuItems() {
  const ctx = useTerminalFirst();
  if (!ctx || !ctx.isTerminalFirst) return null;

  const { view, setView, terminalStartingUp } = ctx;
  const terminalLabel = terminalStartingUp ? "Terminal (starting up…)" : "Terminal";

  return (
    <>
      <DropdownMenuItem
        className="gap-2.5 px-2.5 py-2 text-ui"
        onSelect={() => setView("chat")}
        data-testid="view-mode-menu-chat"
      >
        <MessagesSquareIcon className="size-4" />
        Chat
        {view === "chat" && <CheckIcon className="ml-auto size-4" />}
      </DropdownMenuItem>
      <DropdownMenuItem
        className="gap-2.5 px-2.5 py-2 text-ui"
        onSelect={() => setView("terminal")}
        data-testid="view-mode-menu-terminal"
      >
        {terminalStartingUp ? (
          <Loader2Icon className="size-4 animate-spin" aria-hidden />
        ) : (
          <TerminalIcon className="size-4" />
        )}
        {terminalLabel}
        {view === "terminal" && !ctx.isShellView && <CheckIcon className="ml-auto size-4" />}
      </DropdownMenuItem>
      <DropdownMenuSeparator />
    </>
  );
}
