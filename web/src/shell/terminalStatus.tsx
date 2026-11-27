import {
  type ConnectionState,
  isTerminalProcessExitedClose,
  isUnexpectedTerminalClose,
  WS_CLOSE_WRONG_REPLICA,
} from "@/lib/terminalCloseCodes";
import { terminalLaunchMode, type TerminalInfo } from "@/lib/terminals";
import { cn } from "@/lib/utils";

/**
 * Derived per-terminal status shown in terminal selectors.
 *
 * The terminal resource's ``running`` flag is per-terminal. The live
 * WebSocket bridge state and activity timestamps are only known for
 * mounted terminals.
 */
export type TerminalStatus =
  | "active"
  | "idle"
  | "connecting"
  | "reconnecting"
  | "disconnected"
  | "error"
  | "exited"
  | "closed";

export const STATUS_CONFIG: Record<TerminalStatus, { label: string; className: string }> = {
  active: { label: "Active", className: "bg-emerald-500" },
  idle: { label: "Idle", className: "bg-muted-foreground/55" },
  connecting: { label: "Connecting", className: "bg-amber-500 animate-pulse" },
  reconnecting: { label: "Reconnecting", className: "bg-amber-500 animate-pulse" },
  disconnected: { label: "Disconnected", className: "bg-amber-500" },
  error: { label: "Error", className: "bg-red-500" },
  exited: { label: "Exited", className: "bg-black dark:bg-white" },
  closed: { label: "Closed", className: "bg-black dark:bg-white" },
};

/**
 * Render a visible status dot and label for a terminal tab or selector row.
 *
 * :param status: Terminal-local display status, e.g. ``"idle"``.
 */
export function TerminalStatusBadge({ status }: { status: TerminalStatus }) {
  const { label, className } = STATUS_CONFIG[status];
  return (
    <span
      aria-label={label}
      title={label}
      className="inline-flex shrink-0 items-center gap-1 text-muted-foreground text-sm"
    >
      <span className={cn("inline-block size-1.5 rounded-full", className)} />
      <span>{label}</span>
    </span>
  );
}

/**
 * Derive the display status for a single terminal.
 *
 * :param terminal: Terminal resource entry from ``useTerminals``.
 * :param connectionState: Live bridge state for this terminal when it is
 *     mounted. Pass ``null`` for inactive terminals.
 * :param isActive: Best-effort activity flag from recent PTY output.
 * :returns: Terminal display status.
 */
export function deriveTerminalStatus(
  terminal: TerminalInfo,
  connectionState: ConnectionState | null,
  isActive = false,
): TerminalStatus {
  const launching = terminalLaunchMode(terminal.id) !== null;
  if (!terminal.running && !launching) return "exited";
  if (connectionState?.kind === "closed") {
    if (launching && isTerminalProcessExitedClose(connectionState.code)) return "connecting";
    if (isTerminalProcessExitedClose(connectionState.code)) return "exited";
    if (connectionState.reconnecting) return "reconnecting";
    if (
      isUnexpectedTerminalClose(connectionState.code) ||
      connectionState.code === WS_CLOSE_WRONG_REPLICA
    ) {
      return "disconnected";
    }
    return "closed";
  }
  if (connectionState?.kind === "error") return "error";
  if (connectionState?.kind === "connecting") {
    return connectionState.reconnecting ? "reconnecting" : "connecting";
  }
  if (isActive) return "active";
  return "idle";
}
