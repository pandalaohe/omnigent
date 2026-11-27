/**
 * Zero-dependency WebSocket close-code constants and predicates shared between
 * the lazy terminal view/session and shell status badges.
 */

// WebSocket close codes (RFC 6455 reserves 4xxx).
// 4400 signals wrong-replica routing (mirrors ``ws_common.py`` ``WS_CLOSE_WRONG_REPLICA``).
export const WS_CLOSE_WRONG_REPLICA = 4400;
// 4404 signals the terminal process has exited or is no longer running
// (mirrors ``ws_common.py`` ``WS_CLOSE_TERMINAL_NOT_FOUND``).
export const WS_CLOSE_TERMINAL_NOT_FOUND = 4404;

/**
 * Lifecycle state of the terminal WebSocket bridge, surfaced to React for
 * overlays and shell status badges.
 */
export type ConnectionState =
  | { kind: "connecting"; reconnecting?: boolean }
  | { kind: "connected" }
  | { kind: "closed"; reason: string; code: number; reconnecting?: boolean }
  | { kind: "error" };

/**
 * Decide whether a WebSocket close code represents a transport-level drop
 * worth auto-reconnecting, rather than a deliberate close.
 */
export function isUnexpectedTerminalClose(code: number): boolean {
  return (
    code === 1001 ||
    code === 1005 ||
    code === 1006 ||
    code === 1011 ||
    code === 1012 ||
    code === 1013 ||
    code === 1014
  );
}

/**
 * Decide whether a WebSocket close code means the terminal's process exited
 * (or is no longer running), as opposed to a transient transport disconnect.
 */
export function isTerminalProcessExitedClose(code: number): boolean {
  return code === WS_CLOSE_TERMINAL_NOT_FOUND;
}
