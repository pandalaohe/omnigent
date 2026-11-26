// xterm.js view bridged to an agent's tmux session over a WebSocket.
//
// The xterm + WebSocket lifecycle lives in `TerminalSession` (plain
// JS, outside React). This component is a thin shell: a callback ref
// constructs the session when its container node attaches, and a
// layout effect disposes it on unmount (React 18 never calls a ref's
// returned cleanup).

import { Loader2Icon } from "lucide-react";
import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  useSyncExternalStore,
} from "react";
import { useTerminalIsDark } from "@/components/theme/useTerminalIsDark";
import { Button } from "@/components/ui/button";
import { toast } from "sonner";
import {
  isTerminalClipboardWritePending,
  queueTerminalClipboardWrite,
  writeExplicitTerminalClipboardText,
} from "@/lib/terminalClipboardWriter";
import { useTerminalPaste, type TerminalClipboardAction } from "@/lib/terminalPaste";
import {
  getOmnigentServerIdentity,
  isDatabricksWorkspace,
  isHostRoutingEnabled,
  resolveWebSocketUrl,
} from "@/lib/host";
import {
  canRememberTerminalClipboardPreference,
  readTerminalClipboardPreference,
  subscribeTerminalClipboardPreference,
  writeTerminalClipboardPreference,
  type TerminalClipboardPreference,
} from "@/lib/terminalClipboardPreferences";
import { subscribeCodeFont } from "@/lib/codeFontPreferences";
import { canSignInForTerminal, signInForTerminal } from "@/lib/identity";
import { useFileViewer, useWorkspacePaths } from "@/shell/FileViewerContext";
import {
  isTerminalLaunching,
  resolveInitialAttachUrl,
  subscribeTerminalLaunches,
  terminalLaunchMode,
  watchDirectUpgrade,
  withAttachParams,
} from "@/lib/terminals";
import { getSessionHost, markHostKeyless, isHostKeyless } from "@/lib/sessionHost";
import { hostTerminalsPath } from "@/hooks/useHostTerminals";
import { useIsPhone, useTerminalKeyBarGate } from "@/hooks/useIsPhone";
import { useTerminalKeyboardRoom } from "@/hooks/useTerminalKeyboardRoom";
import {
  EMPTY_STICKY_MODIFIERS,
  barHeightPx,
  useMobileAssistantPreferences,
} from "@/lib/mobileAssistantPreferences";
import {
  TerminalClipboardPrompt,
  type TerminalClipboardDecision,
  type TerminalClipboardPromptReason,
} from "./TerminalClipboardPrompt";
import { TerminalKeyRow } from "./TerminalKeyRow";
import { TerminalSelectionHandles } from "./TerminalSelectionHandles";
import type { SelectionHandles } from "@/lib/terminalTouchSelection";
import {
  TERMINAL_COPY_BLOCKED_MESSAGE,
  TerminalCopySheet,
  TerminalPasteBox,
} from "./TerminalTouchClipboard";
import { resolveTerminalKeys, type KeySurface } from "@/lib/terminalKeys";
import {
  type ConnectionState,
  type LivenessProbeResult,
  type TerminalActivityListener,
  type TerminalInputListener,
  applyTerminalCopy,
  isTerminalProcessExitedClose,
  isUnexpectedTerminalClose,
  LIVENESS_PROBE_TIMEOUT_MS,
  resolveTerminalWorkspaceFileLink,
  TerminalSession,
  terminalTheme,
  WS_CLOSE_WRONG_REPLICA,
} from "./TerminalSession";

/**
 * Backoff schedule for automatic re-attach after a transport-level
 * close ({@link isUnexpectedTerminalClose}). One entry per attempt;
 * when the schedule is exhausted the closed overlay stays up and the
 * user falls back to a manual refresh / resume.
 *
 * The cumulative budget must outlast a server outage so a terminal
 * watched through one recovers on its own instead of dead-ending while
 * the backend is still coming back. The fast ramp covers the common
 * case — a Databricks Apps redeploy reroutes the ingress in ~20-25s —
 * and the schedule then holds at 30s for several minutes so a slow
 * redeploy, a stuck rollout, or a longer infra blip still self-heals
 * rather than stranding the user on a manual refresh. ~5 min total.
 * (A backgrounded tab also re-dials with a fresh budget on the
 * visibilitychange reveal, so this budget is really about a foreground
 * terminal the user is actively watching.)
 *
 * Exported for direct unit testing (fake timers advance through it).
 */
export const RECONNECT_BACKOFF_MS = [
  // Fast ramp: recover promptly from the common ~20-25s redeploy.
  500, 1000, 2000, 4000, 8000, 15000,
  // Then hold at 30s for the rest of a ~5-minute budget, so a longer
  // outage still auto-recovers at a calm cadence instead of dead-ending.
  30000, 30000, 30000, 30000, 30000, 30000, 30000, 30000, 30000,
] as const;

/**
 * A connection that stayed open at least this long before dropping is
 * treated as a fresh outage: the retry budget resets. Without this, a
 * terminal that reconnects fine but drops again hours later (another
 * background-tab freeze) would eventually exhaust the budget; with a
 * plain reset-on-connect, a connect→drop hot loop would retry forever.
 */
export const RECONNECT_STABLE_MS = 30_000;

/** Closes an attach can get when it arrives before its shell exists. */
const LAUNCH_REFUSAL_CODES: ReadonlySet<number> = new Set([4404, 4500]);

const TERMINAL_REFUSAL_MESSAGES: Readonly<Record<string, string>> = {
  origin_untrusted: "Open this terminal from a trusted server address.",
  runner_credential: "Sign in through the web app to use this terminal.",
  browser_session_missing: "Sign in again to use this terminal.",
  browser_account_mismatch: "Sign in with the session owner's account.",
  not_owner: "Only the session owner can control this terminal.",
  admin_not_allowed: "Administrator terminal access is not allowed.",
  access_denied: "Terminal access was denied.",
};

function withReconnectContext(state: ConnectionState, reconnecting: boolean): ConnectionState {
  if (state.kind === "connecting") {
    return reconnecting ? { kind: "connecting", reconnecting: true } : { kind: "connecting" };
  }
  if (state.kind === "closed") {
    return reconnecting
      ? { kind: "closed", reason: state.reason, code: state.code, reconnecting: true }
      : { kind: "closed", reason: state.reason, code: state.code };
  }
  return state;
}

function isReconnectableTerminalClose(code: number): boolean {
  return (
    isUnexpectedTerminalClose(code) || (code === WS_CLOSE_WRONG_REPLICA && !isDatabricksWorkspace())
  );
}

interface TerminalClipboardRequest {
  scope: string;
  epoch: number;
  generation: number;
  text: string;
  source: "terminal" | "selection";
}

// Host terminals always use the owner-checked relay, never direct attach.
type TerminalViewProps = (
  | { sessionId: string; hostId?: never; directAttachUrl?: string }
  | { hostId: string; sessionId?: never; directAttachUrl?: never }
) & {
  /** Opaque terminal resource id, e.g. ``"terminal_bash_s1"``. */
  terminalId: string;
  /** If true, drops keyboard input and runs ``tmux attach -r``. */
  readOnly?: boolean;
  /**
   * Called on every connection-state transition so parents can reflect
   * the terminal's live status. Uses a ref internally so changing the
   * callback never recreates the WebSocket session. Called with ``null``
   * on unmount so callers can clear stale bridge state.
   */
  onStateChange?: (state: ConnectionState | null) => void;
  /**
   * Called when output arrives from the terminal bridge. Best-effort
   * activity signal for UI chrome; idle is inferred by the parent.
   */
  onActivity?: TerminalActivityListener;
  /** Called when keyboard input is sent to the terminal. */
  onInput?: TerminalInputListener;
  /** Optional action shown beside the closed-bridge message. */
  onResume?: () => void | Promise<void>;
  /**
   * What that action resumes, named in its label and errors: "sandbox" when
   * the sandbox itself is offline.
   */
  resumeTarget?: "session" | "sandbox";
  /** Whether the optional resume action is currently in flight. */
  resumePending?: boolean;
  /** When provided, the parent increments this after successful recovery to redial a closed bridge. */
  resumeGeneration?: number;
  /**
   * False while the surface is mounted but hidden (a pre-warmed attach
   * kept alive behind the chat view). The session stays connected either
   * way; on the hidden→visible edge the terminal takes keyboard focus —
   * the WS-open auto-focus is a browser no-op on a hidden element.
   * Default true.
   */
  active?: boolean;
  /**
   * Whether the terminal grabs keyboard focus when its WS connects. Defaults
   * to ``active`` — a foreground surface claims the keyboard as it comes up.
   * The workspace-rail shell overrides this to false so a shell restored on a
   * session switch connects in the background without yanking focus off the
   * chat composer; it stays fully interactive (clipboard, reconnect) either
   * way. It still grabs focus on the reveal edge and on an explicit open.
   */
  focusOnConnect?: boolean;
  /**
   * Whether this surface owns bottom safe-area reservation when the software
   * keyboard is down. False on HostTerminalsPage where the page root already
   * reserves `--omnigent-safe-bottom`. Default true.
   */
  reservesBottomSafeArea?: boolean;
  /**
   * Whether phone key rows may render inside this TerminalView. False on
   * unfocused split panes and the desktop workspace rail. Default true.
   */
  showKeyRows?: boolean;
  keySurface?: KeySurface;
};

export function TerminalView({
  sessionId,
  hostId,
  terminalId,
  readOnly = false,
  onStateChange,
  onActivity,
  onInput,
  onResume,
  resumeTarget = "session",
  resumePending = false,
  resumeGeneration,
  active = true,
  focusOnConnect = active,
  reservesBottomSafeArea = true,
  showKeyRows = true,
  keySurface,
  directAttachUrl,
}: TerminalViewProps) {
  const openFile = useFileViewer();
  const workspacePaths = useWorkspacePaths();
  const fileLinkRef = useRef({ openFile, ...workspacePaths });
  fileLinkRef.current = { openFile, ...workspacePaths };
  const notifyFileLink = useCallback((uri: string): boolean => {
    const current = fileLinkRef.current;
    if (current.openFile === null) return false;
    const target = resolveTerminalWorkspaceFileLink(uri, current.root, current.home);
    if (target === null) return false;
    if (target.line === null) current.openFile(target.path);
    else {
      const { line, endLine } = target;
      current.openFile(target.path, { line, ...(endLine ? { endLine } : {}) });
    }
    return true;
  }, []);
  const [state, setState] = useState<ConnectionState>({ kind: "connecting" });
  const stateRef = useRef(state);
  stateRef.current = state;
  const [connectAttempt, setConnectAttempt] = useState(0);
  const [resumeError, setResumeError] = useState<string | null>(null);
  const clipboardServerIdentity = getOmnigentServerIdentity();
  const clipboardScope = JSON.stringify([
    clipboardServerIdentity,
    hostId ? ["host", hostId] : ["session", sessionId],
    terminalId,
    readOnly,
  ]);
  const [clipboardPrompt, setClipboardPrompt] = useState<
    | (TerminalClipboardRequest & {
        reason: TerminalClipboardPromptReason;
        copyFailed?: boolean;
      })
    | null
  >(null);
  const clipboardScopeRef = useRef({ scope: clipboardScope, epoch: 0 });
  // Unremembered choices apply to one mounted terminal; saved choices apply
  // to every terminal on this server in the current browser or app.
  const clipboardConsentRef = useRef<{
    scope: string;
    decision: TerminalClipboardPreference;
  }>({ scope: clipboardScope, decision: readTerminalClipboardPreference() });
  const clipboardNeedsClickRef = useRef(false);
  const clipboardRequestGenerationRef = useRef(0);
  const clipboardMountedRef = useRef(true);
  const clipboardActiveRef = useRef(active);
  const [clipboardScopeEpoch, setClipboardScopeEpoch] = useState(0);
  useLayoutEffect(() => {
    if (clipboardActiveRef.current !== active) {
      clipboardActiveRef.current = active;
      clipboardRequestGenerationRef.current += 1;
      if (!active) {
        setClipboardPrompt(null);
      }
    }
    if (clipboardScopeRef.current.scope !== clipboardScope) {
      const epoch = clipboardScopeRef.current.epoch + 1;
      clipboardScopeRef.current = { scope: clipboardScope, epoch };
      clipboardConsentRef.current = {
        scope: clipboardScope,
        decision: readTerminalClipboardPreference(),
      };
      clipboardNeedsClickRef.current = false;
      clipboardRequestGenerationRef.current += 1;
      setClipboardPrompt(null);
      setClipboardScopeEpoch(epoch);
    }
  }, [active, clipboardScope]);
  useEffect(
    () =>
      subscribeTerminalClipboardPreference((decision) => {
        const { scope, epoch } = clipboardScopeRef.current;
        const previousGeneration = clipboardRequestGenerationRef.current;
        const generation = (clipboardRequestGenerationRef.current += 1);
        clipboardConsentRef.current = { scope, decision };
        clipboardNeedsClickRef.current = false;
        // Shared grants keep waiting text visible; revocations discard it.
        const keepPendingRequest =
          decision === "allow" && clipboardMountedRef.current && clipboardActiveRef.current;
        setClipboardPrompt((current) =>
          keepPendingRequest &&
          current?.scope === scope &&
          current.epoch === epoch &&
          current.generation === previousGeneration
            ? {
                ...current,
                generation,
                reason: current.reason === "consent" ? "permission" : current.reason,
              }
            : null,
        );
      }),
    [clipboardServerIdentity],
  );
  // True between an unexpected close and the re-dial it scheduled, so
  // the overlay reads "Reconnecting…" instead of the dead-end
  // "Bridge closed" message during automatic recovery.
  const [reconnectPending, setReconnectPending] = useState(false);
  const reconnectPendingRef = useRef(false);
  // Consecutive re-dial attempts in the current outage. A ref, not
  // state: it only feeds the scheduling effect, never the render.
  const reconnectAttemptsRef = useRef(0);
  // Whether the current attach was dialed while its shell's create was in
  // flight (``await_launch``); such an attach gets one re-dial on 4404.
  const dialedDuringLaunchRef = useRef(false);
  // Epoch ms when the current connection opened, or null while down.
  // Lets the close handler tell "stable connection finally dropped"
  // (reset the budget) from "re-dial died straight away" (burn it).
  const connectedAtRef = useRef<number | null>(null);
  // Monotonic dial sequence and hide-snapshot so foreground recovery can tell
  // whether a connecting dial predates the hide without same-millisecond ties,
  // plus a short wake-burst guard so one return never bypasses backoff twice.
  const dialSeqRef = useRef(0);
  const connectStartedAtRef = useRef(0);
  const hiddenAtDialSeqRef = useRef<number | null>(null);
  const wakeRecoveredAtRef = useRef<number | null>(null);
  const reconnectTimerRef = useRef<number | null>(null);
  // A Settings change re-themes a live terminal through the setTheme effect below.
  const isDark = useTerminalIsDark();
  const terminalBackground = terminalTheme(isDark).background;
  // Stable ref so the theme-update effect can reach the live session
  // without adding isDark to the attachSession deps (which would
  // reconnect the WebSocket on every theme change).
  const isDarkRef = useRef(isDark);
  isDarkRef.current = isDark;
  const sessionRef = useRef<TerminalSession | null>(null);
  const terminalPaste = useTerminalPaste(sessionRef, clipboardScope, active, readOnly);
  const cancelPaste = terminalPaste.cancel;
  const [copySheet, setCopySheet] = useState<string | null>(null);
  const [selectionHandles, setSelectionHandles] = useState<SelectionHandles | null>(null);
  useLayoutEffect(() => setCopySheet(null), [clipboardScope, active]);
  const terminalNodeRef = useRef<HTMLDivElement | null>(null);
  const rootRef = useRef<HTMLDivElement | null>(null);
  const isPhone = useIsPhone();
  const isPhoneRef = useRef(isPhone);
  isPhoneRef.current = isPhone;
  // Track whether this terminal has already tried a keyless re-dial after a
  // 4400 wrong-replica close. If keyless still fails with 4400, the host is
  // genuinely unreachable — stop retrying.
  const keylessRef = useRef(false);
  // The reconnect effect below owns the decision; replicating it here keeps the
  // bar mounted (keys disabled in place) across a transport drop instead of
  // unmounting for the frame before `reconnectPending` is set, which would
  // reset the key-row page and blink the terminal height.
  const willAutoReconnect =
    state.kind === "closed" &&
    ((state.code === WS_CLOSE_WRONG_REPLICA && isDatabricksWorkspace() && !keylessRef.current) ||
      (isReconnectableTerminalClose(state.code) &&
        (reconnectAttemptsRef.current < RECONNECT_BACKOFF_MS.length ||
          (connectedAtRef.current !== null &&
            Date.now() - connectedAtRef.current >= RECONNECT_STABLE_MS))));
  const terminalEnded = state.kind === "closed" && !reconnectPending && !willAutoReconnect;
  const keyPreferences = useMobileAssistantPreferences();
  const gate = useTerminalKeyBarGate();
  const canTouchSelect = gate && keySurface !== undefined && active && state.kind === "connected";
  const terminalKeys = keySurface
    ? resolveTerminalKeys({ ...keyPreferences }).keys[keySurface]
    : undefined;
  const hasTerminalBarKeys =
    terminalKeys &&
    (terminalKeys.top.keys.length > 0 ||
      (terminalKeys.bottom.enabled && terminalKeys.bottom.keys.length > 0));
  const canShowKeyRows = Boolean(
    gate &&
    !readOnly &&
    showKeyRows &&
    active &&
    !terminalEnded &&
    terminalKeys?.enabled &&
    hasTerminalBarKeys,
  );
  const { overlap, keyboardUp, roomPx, keyRowWidth, paddingBottomPx } = useTerminalKeyboardRoom(
    rootRef,
    canShowKeyRows,
    reservesBottomSafeArea,
  );
  const barHeight = terminalKeys ? barHeightPx(terminalKeys, keyRowWidth >= 560) : 0;
  const hasBar =
    canShowKeyRows &&
    (terminalKeys?.showRows !== "keyboard" || keyboardUp) &&
    roomPx >= barHeight + 108;

  useEffect(() => {
    const session = sessionRef.current;
    const reset = () => session?.setStickyModifiers({ ...EMPTY_STICKY_MODIFIERS });
    const onVisibility = () => {
      if (document.visibilityState === "hidden") reset();
    };
    if (state.kind !== "connected" || !active || readOnly || !gate) reset();
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      document.removeEventListener("visibilitychange", onVisibility);
      reset();
    };
  }, [state.kind, active, readOnly, sessionId, hostId, terminalId, gate]);

  // Stable refs so callback prop changes never recreate the WS session.
  const onStateChangeRef = useRef(onStateChange);
  onStateChangeRef.current = onStateChange;
  const onActivityRef = useRef(onActivity);
  onActivityRef.current = onActivity;
  const onInputRef = useRef(onInput);
  onInputRef.current = onInput;
  const activeRef = useRef(active);
  activeRef.current = active;
  const focusOnConnectRef = useRef(focusOnConnect);
  focusOnConnectRef.current = focusOnConnect;

  // Bumped by every attach so an in-flight attach can tell it has been
  // superseded — a ref callback can re-run for the *same* node, which
  // leaves no other way to retire the previous attempt's async work.
  const attachGenerationRef = useRef(0);
  // Abort handle for the outgoing attach's direct-upgrade probe, which
  // otherwise holds a loopback socket open for its full timeout.
  const upgradeCtlRef = useRef<AbortController | null>(null);
  const mountedRef = useRef(false);

  useEffect(() => {
    clipboardMountedRef.current = true;
    return () => {
      clipboardMountedRef.current = false;
      clipboardRequestGenerationRef.current += 1;
    };
  }, []);

  // Stable dispatcher: updates local state and notifies the parent.
  const notifyState = useCallback(
    (next: ConnectionState) => {
      // Update stateRef synchronously so microtask continuations (e.g. an
      // in-flight liveness probe) never act on a stale "connected" state.
      stateRef.current = next;
      if (next.kind !== "connected") cancelPaste(next.kind === "closed" || next.kind === "error");
      setState(next);
      const earlyLaunchRefusal =
        terminalLaunchMode(terminalId) === "attach" &&
        next.kind === "closed" &&
        LAUNCH_REFUSAL_CODES.has(next.code);
      onStateChangeRef.current?.(
        earlyLaunchRefusal
          ? { kind: "connecting" }
          : withReconnectContext(next, reconnectPendingRef.current),
      );
    },
    [cancelPaste, terminalId],
  );

  const notifyActivity = useCallback(() => {
    // Terminal output: the attach reached a live shell, so a later close is
    // not a launch refusal.
    dialedDuringLaunchRef.current = false;
    onActivityRef.current?.();
  }, []);

  const notifyInput = useCallback(() => {
    onInputRef.current?.();
  }, []);

  const queueSessionClipboardCopy = useCallback(
    (request: TerminalClipboardRequest & { kind: "automatic" | "user" }) => {
      queueTerminalClipboardWrite({
        text: request.text,
        isCurrent: () =>
          clipboardMountedRef.current &&
          clipboardActiveRef.current &&
          clipboardRequestGenerationRef.current === request.generation &&
          clipboardScopeRef.current.scope === request.scope &&
          clipboardScopeRef.current.epoch === request.epoch &&
          (request.kind === "user" || clipboardConsentRef.current.decision === "allow"),
        onResult: (copied) => {
          if (copied) {
            clipboardNeedsClickRef.current = false;
            toast.success("Copied from terminal.", { duration: 1500 });
          } else {
            clipboardNeedsClickRef.current = true;
            setClipboardPrompt({
              ...request,
              reason: "browser",
              copyFailed: request.kind === "user",
            });
          }
        },
      });
    },
    [],
  );

  const isCopyAllowed = useCallback(
    () =>
      clipboardMountedRef.current &&
      clipboardActiveRef.current &&
      clipboardScopeRef.current.scope === clipboardScope &&
      clipboardScopeRef.current.epoch === clipboardScopeEpoch &&
      clipboardConsentRef.current.decision !== "block",
    [clipboardScope, clipboardScopeEpoch],
  );

  const copyExplicit = useCallback(
    (text: string, isGestureCurrent?: () => boolean) => {
      clipboardRequestGenerationRef.current += 1;
      setClipboardPrompt(null);
      const isAllowed = () => isCopyAllowed() && (isGestureCurrent?.() ?? true);
      void writeExplicitTerminalClipboardText(text, isAllowed).then((copied) => {
        if (!isAllowed()) return;
        if (copied && isGestureCurrent) toast.success("Copied", { duration: 1500 });
        if (!copied)
          toast.error(isGestureCurrent ? "Couldn't copy" : "Couldn't copy terminal text");
      });
    },
    [isCopyAllowed],
  );

  const copyTouchSelection = useRef<(text: string, isCurrent: () => boolean) => void>(() => {});
  copyTouchSelection.current = (text, isCurrent) => {
    if (!isCurrent()) return;
    if (!isCopyAllowed()) {
      toast.info("Copy blocked by the terminal clipboard setting", {
        id: "terminal-clipboard-blocked",
      });
      return;
    }
    copyExplicit(text, isCurrent);
  };

  useLayoutEffect(() => {
    const session = sessionRef.current;
    if (!canTouchSelect || !session) return;
    let enabled = true;
    const isCurrent = () => enabled && sessionRef.current === session && mountedRef.current;
    session.setTouchSelection({
      copy: (text) => copyTouchSelection.current(text, isCurrent),
      handles: setSelectionHandles,
    });
    return () => {
      enabled = false;
      session.setTouchSelection(null);
    };
  }, [canTouchSelect]);

  const notifyClipboardRequest = useCallback(
    (text: string, copyEvent?: ClipboardEvent) => {
      if (
        !clipboardMountedRef.current ||
        !clipboardActiveRef.current ||
        (readOnly && !copyEvent) ||
        clipboardScopeRef.current.scope !== clipboardScope ||
        clipboardScopeRef.current.epoch !== clipboardScopeEpoch
      ) {
        return;
      }
      const generation = (clipboardRequestGenerationRef.current += 1);
      if (clipboardConsentRef.current.scope !== clipboardScope) {
        clipboardConsentRef.current = {
          scope: clipboardScope,
          decision: readTerminalClipboardPreference(),
        };
      }
      if (clipboardConsentRef.current.decision === "block") {
        toast.info(TERMINAL_COPY_BLOCKED_MESSAGE, {
          id: "terminal-clipboard-blocked",
          description: "Change this in Settings → General.",
        });
        return;
      }
      const source = copyEvent ? "selection" : "terminal";
      if (
        clipboardConsentRef.current.decision === "allow" &&
        copyEvent?.clipboardData &&
        !isTerminalClipboardWritePending()
      ) {
        // A pending write can overwrite a native copy, so queue the selection in that case.
        setClipboardPrompt(null);
        clipboardNeedsClickRef.current = false;
        applyTerminalCopy(copyEvent, text);
        return;
      }
      if (
        clipboardConsentRef.current.decision === "allow" &&
        (copyEvent || !clipboardNeedsClickRef.current)
      ) {
        setClipboardPrompt(null);
        queueSessionClipboardCopy({
          scope: clipboardScope,
          epoch: clipboardScopeEpoch,
          generation,
          text,
          source,
          kind: "automatic",
        });
        return;
      }
      // Keep only the newest request while the prompt is open.
      setClipboardPrompt({
        scope: clipboardScope,
        epoch: clipboardScopeEpoch,
        generation,
        text,
        source,
        reason: clipboardConsentRef.current.decision === "allow" ? "browser" : "consent",
      });
    },
    [clipboardScope, clipboardScopeEpoch, queueSessionClipboardCopy, readOnly],
  );

  const onClipboardActionRef = useRef<TerminalClipboardAction>(() => {});
  onClipboardActionRef.current = (op, options, nativeText) => {
    if (op !== "copy") {
      void terminalPaste.paste(op, options, nativeText);
      return;
    }
    if (!active) return;
    if (!isCopyAllowed()) {
      toast.info(TERMINAL_COPY_BLOCKED_MESSAGE, { id: "terminal-clipboard-blocked" });
      return;
    }
    const session = sessionRef.current;
    if (!session) return;
    const selection = session.selectedText();
    if (selection) copyExplicit(selection);
    else if (gate) setCopySheet(session.bufferText());
  };

  const handleClipboardConsent = useCallback(
    (decision: TerminalClipboardDecision | "retry", remember = false) => {
      const prompt =
        clipboardMountedRef.current &&
        clipboardActiveRef.current &&
        (!readOnly || clipboardPrompt?.source === "selection") &&
        clipboardPrompt?.scope === clipboardScope &&
        clipboardPrompt.epoch === clipboardScopeEpoch &&
        clipboardPrompt.generation === clipboardRequestGenerationRef.current
          ? clipboardPrompt
          : null;
      if (prompt === null) return;
      setClipboardPrompt(null);
      if (remember && (decision === "allow" || decision === "block")) {
        if (!writeTerminalClipboardPreference(decision)) {
          toast.error(
            "Couldn't remember your clipboard choice. It applies only to this open terminal.",
            {
              duration: Number.POSITIVE_INFINITY,
              closeButton: true,
            },
          );
        }
      }
      // Saving can synchronously invalidate requests in every mounted terminal.
      const generation = (clipboardRequestGenerationRef.current += 1);
      if (decision !== "retry") {
        clipboardConsentRef.current = {
          scope: clipboardScope,
          decision: decision === "once" ? "ask" : decision,
        };
      }
      if (decision !== "block") {
        queueSessionClipboardCopy({
          scope: clipboardScope,
          epoch: clipboardScopeEpoch,
          generation,
          text: prompt.text,
          source: prompt.source,
          kind: "user",
        });
      }
      sessionRef.current?.focus();
    },
    [clipboardPrompt, clipboardScope, clipboardScopeEpoch, queueSessionClipboardCopy, readOnly],
  );

  const dismissClipboardPrompt = useCallback(() => {
    clipboardRequestGenerationRef.current += 1;
    setClipboardPrompt(null);
    sessionRef.current?.focus();
  }, []);

  const visibleClipboardPrompt =
    clipboardPrompt?.scope === clipboardScope &&
    clipboardPrompt.epoch === clipboardScopeEpoch &&
    clipboardPrompt.generation === clipboardRequestGenerationRef.current &&
    active &&
    (!readOnly || clipboardPrompt.source === "selection")
      ? clipboardPrompt
      : null;

  // Dispose the outgoing session before a remount re-dials. React 18
  // ignores the cleanup function attachSession returns (ref cleanups
  // arrived in React 19), so without this every remount would abandon
  // the previous session — xterm buffers, observers, and all.
  const disposeActiveSession = useCallback(() => {
    cancelPaste();
    setCopySheet(null);
    sessionRef.current?.dispose();
    sessionRef.current = null;
  }, [cancelPaste]);

  useLayoutEffect(() => {
    mountedRef.current = true;
    onStateChangeRef.current?.(stateRef.current);
    return () => {
      mountedRef.current = false;
      // Synchronous so a same-id successor's first report lands after it.
      onStateChangeRef.current?.(null);
      // Deferred so StrictMode's dev-only remount keeps the live attach.
      queueMicrotask(() => {
        if (mountedRef.current) return;
        upgradeCtlRef.current?.abort();
        disposeActiveSession();
      });
    };
  }, [disposeActiveSession]);

  const handleResume = useCallback(async () => {
    if (!onResume) return;
    setResumeError(null);
    try {
      await onResume();
      if (resumeGeneration === undefined) {
        disposeActiveSession();
        setConnectAttempt((attempt) => attempt + 1);
      }
    } catch (error) {
      setResumeError(resumeErrorText(error, resumeTarget));
    }
  }, [onResume, disposeActiveSession, resumeTarget, resumeGeneration]);

  // A new shell whose create may be waking the runner opens its tab at once
  // but attaches only once the create returns.
  const launchMode = useSyncExternalStore(subscribeTerminalLaunches, () =>
    terminalLaunchMode(terminalId),
  );
  const waitingForCreate = launchMode === "wait";

  const attachSession = useCallback(
    (node: HTMLDivElement | null) => {
      terminalNodeRef.current = node;
      if (node === null) return;
      // React re-runs a ref callback for the *same* node whenever the
      // callback's identity changes — here, when the runner's
      // direct-attach advert lands after mount. Retire the previous
      // attach before touching the node: otherwise xterm stacks a
      // second instance inside it (two helper textareas, two
      // renderers) and the superseded upgrade watcher later re-dials
      // on top of the session that replaced it.
      const generation = (attachGenerationRef.current += 1);
      upgradeCtlRef.current?.abort();
      disposeActiveSession();
      node.replaceChildren();
      // Reset to ``connecting`` for every fresh attach so a stale
      // overlay from a previous mount doesn't flash during the
      // handshake. The session's WS ``open`` handler transitions us
      // to ``connected``.
      notifyState({ kind: "connecting" });
      if (waitingForCreate) return;
      dialSeqRef.current += 1;
      connectStartedAtRef.current = Date.now();

      // Defer the actual session construction by one microtask so
      // React 19 StrictMode's synchronous attach → cleanup → attach
      // sequence collapses to a single real WS handshake. Without
      // this, the first attach opens a WebSocket, the cleanup closes
      // it 0ms later, and the second attach opens another — the
      // server sees two handshakes per mount in dev. The microtask
      // runs after the entire commit phase: by then the StrictMode
      // cleanup has flipped ``cancelled`` and the first scheduled
      // open is a no-op. The second attach's microtask proceeds and
      // is the one that actually opens the WS.
      let terminalSession: TerminalSession | null = null;
      let cancelled = false;
      const upgradeCtl = new AbortController();
      upgradeCtlRef.current = upgradeCtl;
      // Superseded by a later attach on this node, or unmounted (checked
      // only after the first await)? React 18 never calls the ref cleanup.
      const superseded = () =>
        cancelled || attachGenerationRef.current !== generation || !mountedRef.current;
      void (async () => {
        // The awaited microtask preserves the StrictMode-collapse
        // behavior queueMicrotask provided; the URL resolution (when a
        // direct URL exists) adds real async time, so re-check after
        // every await.
        await Promise.resolve();
        if (superseded()) return;
        // Browser WebSockets carry the host ID in the query string so they
        // reach the replica holding the runner tunnel. Hostless sessions
        // and direct connections need no routing key.
        const computedHostId = (() => {
          if (!isHostRoutingEnabled()) return undefined;
          if (isDatabricksWorkspace() && keylessRef.current) return undefined;
          const h = hostId ?? getSessionHost(sessionId!);
          return h && (!isDatabricksWorkspace() || !isHostKeyless(h)) ? h : undefined;
        })();
        const awaitLaunch = !hostId && isTerminalLaunching(terminalId);
        dialedDuringLaunchRef.current = awaitLaunch;
        const relayUrl = hostId
          ? resolveWebSocketUrl(
              `${hostTerminalsPath(hostId)}/${encodeURIComponent(terminalId)}/attach` +
                (computedHostId ? `?omnigent_slice_key=${encodeURIComponent(computedHostId)}` : ""),
            )
          : buildAttachUrl(sessionId!, terminalId, readOnly, computedHostId, awaitLaunch);
        const directUrl = directAttachUrl ? withAttachParams(directAttachUrl, readOnly) : undefined;
        // Never keep the user waiting on the direct path: this resolves
        // direct only when the loopback listener is already known
        // reachable; otherwise it returns the relay URL immediately.
        const url = await resolveInitialAttachUrl(directUrl, relayUrl);
        if (superseded()) return;
        terminalSession = new TerminalSession(
          node,
          url,
          notifyState,
          isDarkRef.current,
          notifyActivity,
          notifyInput,
          !readOnly && activeRef.current,
          notifyClipboardRequest,
          focusOnConnectRef.current && !isPhoneRef.current,
          terminalId === "terminal_codex_main",
          notifyFileLink,
          (...args) => {
            if (!superseded()) onClipboardActionRef.current(...args);
          },
          url === relayUrl,
        );
        sessionRef.current = terminalSession;
        // Relay-connected with a direct URL on offer: negotiate the
        // loopback upgrade in the background. In Chrome this is what
        // raises the Local Network Access prompt; the probe socket
        // waits out the user's decision behind the live relay session.
        // On success, re-dial — the known-good cache makes the remount
        // pick the direct URL.
        if (directUrl !== undefined && url === relayUrl) {
          const upgraded = await watchDirectUpgrade(directUrl, upgradeCtl.signal);
          if (superseded() || !upgraded) return;
          disposeActiveSession();
          setConnectAttempt((attempt) => attempt + 1);
        }
      })();
      return () => {
        cancelled = true;
        upgradeCtl.abort();
        terminalSession?.dispose();
        sessionRef.current = null;
        if (terminalNodeRef.current === node) terminalNodeRef.current = null;
        onStateChangeRef.current?.(null);
      };
    },
    [
      sessionId,
      hostId,
      terminalId,
      readOnly,
      directAttachUrl,
      notifyState,
      notifyActivity,
      notifyInput,
      notifyClipboardRequest,
      notifyFileLink,
      disposeActiveSession,
      waitingForCreate,
    ],
  );

  // Push theme changes into the live session without remounting.
  useEffect(() => {
    sessionRef.current?.setTheme(isDark);
  }, [isDark]);

  useEffect(() => {
    sessionRef.current?.setClipboardEnabled(!readOnly && active);
  }, [readOnly, active]);

  // An early attach can be refused before its shell exists (4404 before
  // registration, 4500 while the runner is being rebound). After the create
  // succeeds, retry once without ``await_launch``. Deliberate closes (e.g.
  // 4405 detached) and closes after the shell produced output are left alone.
  const closedByOwnLaunch = state.kind === "closed" && LAUNCH_REFUSAL_CODES.has(state.code);
  useEffect(() => {
    if (!dialedDuringLaunchRef.current || launchMode !== null || !closedByOwnLaunch) return;
    dialedDuringLaunchRef.current = false;
    reconnectAttemptsRef.current = 0;
    disposeActiveSession();
    setConnectAttempt((attempt) => attempt + 1);
  }, [launchMode, closedByOwnLaunch, disposeActiveSession]);
  const refusedDuringLaunch = launchMode === "attach" && closedByOwnLaunch;
  const previousResumeGeneration = useRef(resumeGeneration);
  useEffect(() => {
    if (previousResumeGeneration.current === resumeGeneration) return;
    previousResumeGeneration.current = resumeGeneration;
    if (resumeGeneration === undefined) return;
    setResumeError(null);
    const current = stateRef.current;
    if (current.kind === "connected") return;
    reconnectAttemptsRef.current = 0;
    disposeActiveSession();
    setConnectAttempt((attempt) => attempt + 1);
  }, [resumeGeneration, disposeActiveSession]);

  const setReconnectPendingState = useCallback((next: boolean) => {
    const changed = reconnectPendingRef.current !== next;
    reconnectPendingRef.current = next;
    setReconnectPending(next);
    if (changed && mountedRef.current) {
      const current = stateRef.current;
      if (current.kind === "closed" || current.kind === "connecting") {
        onStateChangeRef.current?.(withReconnectContext(current, next));
      }
    }
  }, []);

  // Single redial owner shared by backoff timers, foreground recovery, and
  // manual retry so concurrent wake events cannot stack duplicate dials.
  const startRedial = useCallback(
    (resetAttempts: boolean) => {
      if (reconnectTimerRef.current !== null) {
        window.clearTimeout(reconnectTimerRef.current);
        reconnectTimerRef.current = null;
      }
      if (resetAttempts) reconnectAttemptsRef.current = 0;
      reconnectPendingRef.current = true;
      setReconnectPending(true);
      dialSeqRef.current += 1;
      connectStartedAtRef.current = Date.now();
      stateRef.current = { kind: "connecting" };
      disposeActiveSession();
      setConnectAttempt((attempt) => attempt + 1);
    },
    [disposeActiveSession],
  );

  const triggerReconnect = useCallback(() => startRedial(true), [startRedial]);

  const probeActiveSession = useCallback(() => {
    const session = sessionRef.current;
    if (!session || typeof session.probeLiveness !== "function") return;
    void session.probeLiveness(LIVENESS_PROBE_TIMEOUT_MS).then((result: LivenessProbeResult) => {
      if (
        result !== "dead" ||
        !mountedRef.current ||
        sessionRef.current !== session ||
        stateRef.current.kind !== "connected"
      ) {
        return;
      }
      startRedial(true);
    });
  }, [startRedial]);

  // On the hidden→visible edge of a pre-warmed surface: focus the
  // terminal (the session's WS-open focus is a no-op while the element is
  // hidden — visibility:hidden elements aren't focusable), and if the
  // transport dropped while the surface sat in the background — possibly
  // exhausting the reconnect budget with nobody watching — retry
  // immediately with a fresh budget. Reveal is a user signal, exactly
  // like the visibilitychange redial for frozen tabs. Deliberate closes
  // (4xxx) keep the dead-end overlay as ever.
  const wasActiveRef = useRef(active);
  useEffect(() => {
    if (active && !wasActiveRef.current) {
      if (!isPhoneRef.current) sessionRef.current?.focus();
      const current = stateRef.current;
      if (current.kind === "closed" && isReconnectableTerminalClose(current.code)) {
        startRedial(true);
      } else if (current.kind === "connected") {
        probeActiveSession();
      }
    }
    wasActiveRef.current = active;
  }, [active, startRedial, probeActiveSession]);

  // Returning to foreground on mobile (lock/unlock, app switch, bfcache
  // restore, or regaining network): probe a seemingly-connected socket for a
  // half-open drop, redial a disconnected bridge immediately with a fresh
  // budget, and restart a dial that stalled across sleep. Deliberate closes
  // (process exited 4404, detached 4405, refused 4403) are never resurrected.
  useEffect(() => {
    if (waitingForCreate) return;
    const recoverOnForeground = (
      trigger: "visibility" | "pageshow" | "online",
      hiddenAtDialSeq: number | null,
    ) => {
      if (!mountedRef.current) return;
      const now = Date.now();
      if (
        wakeRecoveredAtRef.current !== null &&
        now - wakeRecoveredAtRef.current < LIVENESS_PROBE_TIMEOUT_MS
      ) {
        return;
      }
      const current = stateRef.current;
      if (current.kind === "closed") {
        const canRedial =
          isReconnectableTerminalClose(current.code) ||
          (current.code === WS_CLOSE_WRONG_REPLICA &&
            isDatabricksWorkspace() &&
            !keylessRef.current);
        if (canRedial) {
          wakeRecoveredAtRef.current = now;
          startRedial(true);
        }
        return;
      }
      if (!activeRef.current) return;
      if (current.kind === "connected") {
        probeActiveSession();
        return;
      }
      const stalledAcrossHide = hiddenAtDialSeq !== null && dialSeqRef.current <= hiddenAtDialSeq;
      const stalledAge =
        (trigger === "pageshow" || trigger === "online") &&
        now - connectStartedAtRef.current >= LIVENESS_PROBE_TIMEOUT_MS;
      if (stalledAcrossHide || stalledAge) {
        wakeRecoveredAtRef.current = now;
        startRedial(true);
      }
    };
    const onVisibility = () => {
      if (document.visibilityState === "hidden") {
        hiddenAtDialSeqRef.current = dialSeqRef.current;
        wakeRecoveredAtRef.current = null;
        return;
      }
      if (document.visibilityState === "visible") {
        const hiddenAtDialSeq = hiddenAtDialSeqRef.current;
        hiddenAtDialSeqRef.current = null;
        recoverOnForeground("visibility", hiddenAtDialSeq);
      }
    };
    const onPageShow = (event: PageTransitionEvent) => {
      if (event.persisted) {
        const hiddenAtDialSeq = hiddenAtDialSeqRef.current;
        hiddenAtDialSeqRef.current = null;
        recoverOnForeground("pageshow", hiddenAtDialSeq);
      }
    };
    const onOnline = () => {
      if (document.visibilityState === "visible") {
        recoverOnForeground("online", null);
      }
    };
    const onOffline = () => {
      wakeRecoveredAtRef.current = null;
    };
    document.addEventListener("visibilitychange", onVisibility);
    window.addEventListener("pageshow", onPageShow);
    window.addEventListener("online", onOnline);
    window.addEventListener("offline", onOffline);
    return () => {
      document.removeEventListener("visibilitychange", onVisibility);
      window.removeEventListener("pageshow", onPageShow);
      window.removeEventListener("online", onOnline);
      window.removeEventListener("offline", onOffline);
    };
  }, [waitingForCreate, startRedial, probeActiveSession]);

  // Push code-font changes (Settings → Appearance) into the live session the
  // same way — xterm is a fixed-pixel widget, so it can't follow a CSS variable
  // like the chrome font and must be told imperatively. The subscription
  // outlives individual re-dials (sessionRef is swapped in place), and a fresh
  // session reads the current pref at construction, so a change made while
  // disconnected still lands on reconnect.
  useEffect(() => {
    return subscribeCodeFont((font) => {
      sessionRef.current?.setFont(font);
    });
  }, []);

  // Auto-reconnect on transport-level drops (background-tab freezes,
  // server restarts — see isUnexpectedTerminalClose). Deliberate
  // closes keep the dead-end overlay so a terminal the server ended
  // isn't resurrected in a loop. The re-dial reuses the resume path's
  // remount: bumping connectAttempt swaps the keyed mount node, which
  // disposes the dead session and attaches a fresh one — tmux
  // re-renders the full screen on attach, so nothing visible is lost.
  useEffect(() => {
    if (state.kind === "connected") {
      connectedAtRef.current = Date.now();
      wakeRecoveredAtRef.current = null;
      setReconnectPendingState(false);
      return;
    }
    // "connecting" (a re-dial in flight) keeps the pending flag;
    // "error" is transient and always followed by a close event.
    if (state.kind !== "closed") return;
    // Wrong-replica close (4400): the keyed request reached the wrong replica.
    // Mark the host keyless so the next dial skips the key, and re-dial
    // immediately without backoff (the correct route is one handshake away).
    // One-shot: if we're ALREADY keyless and still get 4400, the host is
    // genuinely unreachable from here — stop, don't loop.
    if (state.code === WS_CLOSE_WRONG_REPLICA && isDatabricksWorkspace()) {
      if (keylessRef.current) {
        setReconnectPendingState(false);
        return;
      }
      keylessRef.current = true;
      const routingHostId = hostId ?? getSessionHost(sessionId!);
      if (routingHostId) markHostKeyless(routingHostId);
      startRedial(false);
      return;
    }
    if (!isReconnectableTerminalClose(state.code)) {
      setReconnectPendingState(false);
      return;
    }
    // A connection that survived RECONNECT_STABLE_MS before dropping
    // is a fresh outage — restore the full retry budget before
    // charging this attempt.
    if (
      connectedAtRef.current !== null &&
      Date.now() - connectedAtRef.current >= RECONNECT_STABLE_MS
    ) {
      reconnectAttemptsRef.current = 0;
    }
    connectedAtRef.current = null;
    if (reconnectAttemptsRef.current >= RECONNECT_BACKOFF_MS.length) {
      setReconnectPendingState(false);
      return;
    }
    const delay = RECONNECT_BACKOFF_MS[reconnectAttemptsRef.current];
    reconnectAttemptsRef.current += 1;
    setReconnectPendingState(true);
    const timer = window.setTimeout(() => {
      reconnectTimerRef.current = null;
      startRedial(false);
    }, delay);
    reconnectTimerRef.current = timer;
    return () => {
      window.clearTimeout(timer);
      if (reconnectTimerRef.current === timer) reconnectTimerRef.current = null;
    };
  }, [state, startRedial, setReconnectPendingState, sessionId, hostId]);

  // `isolate` scopes the status overlay's z-index to this surface so it
  // can't paint over body-portaled UI such as the header's session menu.
  return (
    <div
      ref={rootRef}
      data-testid="terminal-view"
      data-state={state.kind}
      data-terminal-id={terminalId}
      data-terminal-theme={isDark ? "dark" : "light"}
      data-keyboard-overlap={canShowKeyRows ? String(overlap) : undefined}
      data-keyboard-up={canShowKeyRows ? String(keyboardUp) : undefined}
      className="relative isolate flex min-h-0 flex-1 flex-col @container [:is([data-ios-native],[data-android-native])_[data-testid=terminals-panel]_&]:[--term-safe-b:0px] [:is([data-ios-native],[data-android-native])_[data-testid=terminals-panel]_&]:[--key-row-safe-l:0px] [:is([data-ios-native],[data-android-native])_[data-testid=terminals-panel]_&]:[--key-row-safe-r:0px]"
      style={canShowKeyRows ? { paddingBottom: `${paddingBottomPx}px` } : undefined}
    >
      {visibleClipboardPrompt !== null && (
        <TerminalClipboardPrompt
          reason={visibleClipboardPrompt.reason}
          canRemember={canRememberTerminalClipboardPreference()}
          copyFailed={visibleClipboardPrompt.copyFailed === true}
          onDecision={handleClipboardConsent}
          onRetry={() => handleClipboardConsent("retry")}
          onDismiss={dismissClipboardPrompt}
        />
      )}
      <div
        className="relative min-h-0 flex-1 overflow-hidden p-3"
        style={{ backgroundColor: terminalBackground }}
      >
        <div key={connectAttempt} ref={attachSession} className="h-full w-full overflow-hidden" />
        {canTouchSelect && selectionHandles && (
          <TerminalSelectionHandles
            positions={selectionHandles}
            onStart={(handle, event) =>
              sessionRef.current?.startTouchSelectionHandle(handle, event)
            }
          />
        )}
        {state.kind !== "connected" && (
          <StatusOverlay
            state={refusedDuringLaunch ? { kind: "connecting" } : state}
            startingShell={waitingForCreate || refusedDuringLaunch}
            reconnectPending={reconnectPending}
            onReconnect={
              state.kind === "closed" && isReconnectableTerminalClose(state.code)
                ? triggerReconnect
                : undefined
            }
            onResume={onResume ? handleResume : undefined}
            resumeTarget={resumeTarget}
            resumePending={resumePending}
            resumeError={resumeError}
            onSignedIn={() => {
              reconnectAttemptsRef.current = 0;
              disposeActiveSession();
              setConnectAttempt((attempt) => attempt + 1);
            }}
          />
        )}
      </div>
      {terminalPaste.review && (
        <div className="flex items-center gap-2 border-t bg-background px-3 py-2">
          <div className="min-w-0 flex-1">
            <p role="status" className="text-xs text-muted-foreground">
              {terminalPaste.review.text.split("\n").length} lines · Review before pasting
            </p>
            <pre className="overflow-hidden text-ellipsis whitespace-pre text-xs">
              {terminalPaste.review.text
                .split("\n", 3)
                .join("\n")
                .slice(0, 240)
                .replace(/\t/g, "⇥")}
            </pre>
          </div>
          <Button type="button" size="xs" variant="outline" onClick={() => cancelPaste()}>
            Cancel
          </Button>
          <Button type="button" size="xs" onClick={terminalPaste.confirm}>
            Paste
          </Button>
        </div>
      )}
      {hasBar && (
        <TerminalKeyRow
          sessionRef={sessionRef}
          keyboardUp={keyboardUp}
          disabled={state.kind !== "connected"}
          surface={terminalKeys}
          identityKey={`${keySurface}:${sessionId ?? hostId}:${terminalId}`}
        />
      )}
      <TerminalCopySheet
        key={clipboardScopeEpoch}
        text={copySheet}
        isCopyBlocked={() => !isCopyAllowed()}
        onClose={() => setCopySheet(null)}
      />
      <TerminalPasteBox
        text={terminalPaste.pasteBoxText}
        enabled={state.kind === "connected" && !readOnly}
        onChange={terminalPaste.setPasteBoxText}
        onSend={terminalPaste.pasteFromBox}
        onClose={() => cancelPaste()}
      />
    </div>
  );
}

function StatusOverlay({
  state,
  startingShell,
  reconnectPending,
  onReconnect,
  onResume,
  resumeTarget,
  resumePending,
  resumeError,
  onSignedIn,
}: {
  state: ConnectionState;
  /** True while a new shell waits for its create before attaching. */
  startingShell: boolean;
  /** True while an automatic re-dial is scheduled for a closed bridge. */
  reconnectPending: boolean;
  /** Manual retry action shown after a transport drop exhausts automatic retries. */
  onReconnect?: () => void;
  onResume?: () => void | Promise<void>;
  resumeTarget: "session" | "sandbox";
  resumePending: boolean;
  resumeError: string | null;
  onSignedIn: () => void;
}) {
  const [signInPending, setSignInPending] = useState(false);
  const [signInError, setSignInError] = useState<string | null>(null);
  const refusal =
    state.kind === "closed" && state.code === 4403
      ? TERMINAL_REFUSAL_MESSAGES[
          Object.hasOwn(TERMINAL_REFUSAL_MESSAGES, state.reason) ? state.reason : "access_denied"
        ]
      : undefined;
  const browserSessionMissing =
    refusal !== undefined && state.kind === "closed" && state.reason === "browser_session_missing";
  const needsSignIn = browserSessionMissing && canSignInForTerminal();
  const refusalMessage =
    browserSessionMissing && !needsSignIn
      ? "Sign in again through this server's sign-in page to use this terminal."
      : refusal;
  const showReconnecting =
    !startingShell && reconnectPending && (state.kind === "closed" || state.kind === "connecting");
  const processExited =
    state.kind === "closed" && refusal === undefined && isTerminalProcessExitedClose(state.code);
  const disconnected =
    state.kind === "closed" &&
    refusal === undefined &&
    !processExited &&
    isReconnectableTerminalClose(state.code);
  const recoverSignIn = async () => {
    setSignInPending(true);
    setSignInError(null);
    try {
      const result = await signInForTerminal();
      if (result === "redirecting") return;
      if (result) onSignedIn();
      else setSignInError("Sign-in was not completed. Try again.");
    } catch {
      setSignInError("Sign-in failed. Try again.");
    }
    setSignInPending(false);
  };
  // Render outside the xterm container so close/error messages don't
  // pollute the scrollback buffer the way ANSI-escape writes would.
  return (
    <div className="absolute inset-0 z-[10000] flex items-center justify-center bg-background/85 text-ui text-foreground backdrop-blur-[1px]">
      {state.kind === "connecting" && !showReconnecting && (
        <span className="flex items-center gap-2">
          <Loader2Icon className="size-4 animate-spin" />
          {startingShell ? "Starting shell…" : "Connecting…"}
        </span>
      )}
      {showReconnecting && (
        // An automatic re-dial is scheduled or in flight — show recovery, not
        // the dead-end message, so a transient drop never reads as fatal.
        <span data-testid="terminal-reconnecting" className="flex items-center gap-2">
          <Loader2Icon className="size-4 animate-spin" />
          Disconnected — reconnecting…
        </span>
      )}
      {state.kind === "closed" && !reconnectPending && (
        <div className="flex flex-wrap items-center justify-center gap-2 px-3">
          <span
            role={refusal ? "status" : undefined}
            data-testid={
              processExited
                ? "terminal-process-exited"
                : disconnected
                  ? "terminal-disconnected"
                  : undefined
            }
          >
            {refusalMessage ??
              (processExited
                ? "Process exited"
                : disconnected
                  ? `Disconnected: ${state.reason}`
                  : `Bridge closed: ${state.reason}`)}
          </span>
          {needsSignIn && (
            <Button
              type="button"
              size="xs"
              variant="secondary"
              disabled={signInPending}
              onClick={() => void recoverSignIn()}
              componentId="terminal.sign-in-again"
            >
              {signInPending ? "Signing in…" : "Sign in again"}
            </Button>
          )}
          {signInError && needsSignIn && (
            <span role="alert" className="basis-full text-center text-sm text-destructive">
              {signInError}
            </span>
          )}
          {disconnected && onReconnect && (
            <Button
              type="button"
              size="xs"
              variant="secondary"
              onClick={onReconnect}
              componentId="diagnostics.terminal.reconnect"
            >
              Reconnect
            </Button>
          )}
          {onResume && !refusal && (
            <Button
              type="button"
              size="xs"
              variant="secondary"
              onClick={onResume}
              disabled={resumePending}
              className="border-zinc-500/50 bg-zinc-100 text-zinc-950 hover:bg-white"
              componentId="diagnostics.terminal.resume"
            >
              {resumePending ? "Resuming…" : `Resume ${resumeTarget}`}
            </Button>
          )}
          {resumeError && (
            <span className="basis-full text-center text-sm text-destructive">{resumeError}</span>
          )}
        </div>
      )}
      {state.kind === "error" && <span>Bridge error</span>}
    </div>
  );
}

function resumeErrorText(error: unknown, target: string): string {
  if (error instanceof Error && error.message) return `Couldn't resume ${target}: ${error.message}`;
  return `Couldn't resume ${target}.`;
}

/**
 * Build the path + query for the resource-addressed attach endpoint.
 *
 * The terminal is addressed by its opaque resource id (the server's
 * canonical key), so user-derived names never appear in the path —
 * dodges URL-encoding pitfalls for names with slashes or reserved
 * characters.
 *
 * Pure helper — exported for direct unit testing. Production code
 * should call :func:`buildAttachUrl`.
 *
 * :param sessionId: Session/conversation identifier,
 *     e.g. ``"conv_abc123"``.
 * :param terminalId: Opaque terminal resource id,
 *     e.g. ``"terminal_bash_s1"``.
 * :param readOnly: If true, requests a read-only attach. Forwarded
 *     to the server as ``?read_only=true``.
 * :param hostId: The session's host_id, forwarded as ``?omnigent_slice_key=``.
 * :param awaitLaunch: If true, the terminal's create request is still in
 *     flight; ``?await_launch=true`` asks the server to wait for it.
 * :returns: The path-and-query portion of the WS URL, e.g.
 *     ``"/v1/sessions/.../resources/terminals/.../attach"``.
 */
export function buildAttachPath(
  sessionId: string,
  terminalId: string,
  readOnly: boolean,
  hostId?: string,
  awaitLaunch = false,
): string {
  const path =
    `/v1/sessions/${encodeURIComponent(sessionId)}` +
    `/resources/terminals/${encodeURIComponent(terminalId)}/attach`;
  // Query params are only emitted when set, so the common (unsharded) case
  // keeps URLs short and stable for anything that greps the access log.
  // ``omnigent_slice_key`` pins this WebSocket to the replica holding the
  // tunnel: a browser WS handshake can't carry request headers, so the routing
  // key rides the query string — the one part of the handshake page JS controls
  // — and the server ignores it as an app param.
  const params = new URLSearchParams();
  if (readOnly) params.set("read_only", "true");
  if (hostId) params.set("omnigent_slice_key", hostId);
  if (awaitLaunch) params.set("await_launch", "true");
  const qs = params.toString();
  return qs ? `${path}?${qs}` : path;
}

/**
 * Build the WebSocket URL for the resource-addressed attach endpoint.
 *
 * Uses the current page's origin so the URL works whether the SPA is
 * served from the Omnigent server itself or via the Vite dev proxy.
 * ``ws:``/``wss:`` matches the page's ``http:``/``https:``.
 *
 * :param sessionId: Session/conversation identifier.
 * :param terminalId: Opaque terminal resource id.
 * :param readOnly: If true, requests a read-only attach.
 * :param hostId: The session's host_id, forwarded as the routing key
 *     ``?omnigent_slice_key=``.
 * :param awaitLaunch: Wait for a terminal whose create is still in flight.
 * :returns: The fully-qualified ``ws(s)://`` URL.
 */
function buildAttachUrl(
  sessionId: string,
  terminalId: string,
  readOnly: boolean,
  hostId?: string,
  awaitLaunch = false,
): string {
  // Delegates origin/prefix resolution to the embed host when present
  // (standalone falls back to the current page's origin).
  return resolveWebSocketUrl(buildAttachPath(sessionId, terminalId, readOnly, hostId, awaitLaunch));
}
