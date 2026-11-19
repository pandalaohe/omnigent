// Unit tests for the WebSocket attach path builder and the closed
// bridge overlay.
//
// The production TerminalView component creates xterm + a real
// WebSocket bridge. These tests mock that bridge and drive its state
// callback directly, while still pinning the pure URL builder contract
// the server cares about.

import type * as TerminalSessionModule from "./TerminalSession";
import type * as TerminalsModule from "@/lib/terminals";
import type * as IdentityModule from "@/lib/identity";

import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { StrictMode, act, useMemo, type ReactElement } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Toaster } from "@/components/ui/sonner";
import { FileViewerContext, type OpenFileOptions } from "@/shell/FileViewerContext";
import { toast } from "sonner";
import { signInForTerminal } from "@/lib/identity";
import { resolveTerminalKeys, writeTerminalKeyPreferences } from "@/lib/terminalKeys";
import * as capabilities from "@/lib/capabilities";
import * as nativeBridge from "@/lib/nativeBridge";
import {
  readMobileAssistantPreferences,
  writeMobileAssistantPreferences,
  type TerminalStickyModifiers,
} from "@/lib/mobileAssistantPreferences";
import * as host from "@/lib/host";
import { isHostKeyless, setSessionHost } from "@/lib/sessionHost";
import { resolveInitialAttachUrl, setTerminalLaunching, watchDirectUpgrade } from "@/lib/terminals";
import {
  readTerminalClipboardPreference,
  writeTerminalClipboardPreference,
} from "@/lib/terminalClipboardPreferences";
import type { ConnectionState } from "./TerminalSession";
import type { TerminalClipboardAction } from "@/lib/terminalPaste";
import {
  TerminalView,
  RECONNECT_BACKOFF_MS,
  RECONNECT_STABLE_MS,
  buildAttachPath,
} from "./TerminalView";

const clipboardMock = vi.hoisted(() => ({
  copyText: vi.fn<(text: string) => Promise<void>>(),
}));

vi.mock("@/lib/clipboard", () => ({ copyText: clipboardMock.copyText }));

vi.mock("@/lib/terminals", async (importOriginal) => {
  const real = await importOriginal<typeof TerminalsModule>();
  return {
    ...real,
    resolveInitialAttachUrl: vi.fn(real.resolveInitialAttachUrl),
    watchDirectUpgrade: vi.fn(real.watchDirectUpgrade),
  };
});

function FileViewerHarness({
  openFile,
}: {
  openFile: (path: string, options?: OpenFileOptions) => void;
}) {
  const value = useMemo(
    () => ({
      openFile,
      openGithubTab: () => {},
      isChangedPath: () => false,
      conversationId: "conv_abc",
      workspaceRoot: "/home/u/ws",
      workspaceHome: "/home/u",
    }),
    [openFile],
  );
  return (
    <FileViewerContext.Provider value={value}>
      <TerminalView sessionId="conv_abc" terminalId="terminal_codex_main" />
    </FileViewerContext.Provider>
  );
}

const terminalSessionMock = vi.hoisted(() => ({
  instances: [] as {
    url: string;
    container: HTMLDivElement;
    clipboardEnabled: boolean;
    focusOnConnect: boolean;
    adaptCodexPalette: boolean;
    onClipboardRequest?: (text: string, copyEvent?: ClipboardEvent) => void;
    onFileLink?: (uri: string) => boolean;
    onClipboardAction?: TerminalClipboardAction;
    canPaste: ReturnType<typeof vi.fn>;
    pastesBracketed: ReturnType<typeof vi.fn>;
    readHostClipboard: ReturnType<typeof vi.fn>;
    paste: ReturnType<typeof vi.fn>;
    onState: (state: ConnectionState) => void;
    onActivity?: () => void;
    dispose: ReturnType<typeof vi.fn>;
    setTheme: ReturnType<typeof vi.fn>;
    setClipboardEnabled: ReturnType<typeof vi.fn>;
    sendSoftKey: ReturnType<typeof vi.fn>;
    getStickyModifiers: () => TerminalStickyModifiers;
    focus: ReturnType<typeof vi.fn>;
    blur: ReturnType<typeof vi.fn>;
    isFocused: ReturnType<typeof vi.fn>;
  }[],
}));

vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof IdentityModule>()),
  signInForTerminal: vi.fn(),
}));

describe("terminal access recovery", () => {
  beforeEach(() => {
    vi.spyOn(capabilities, "getCachedServerInfo").mockReturnValue({
      ...capabilities.FALLBACK_SERVER_INFO,
      login_url: "/login",
    });
    vi.spyOn(nativeBridge, "supportsSignInAgain").mockReturnValue(false);
  });

  it.each([
    [null, false, false],
    [" ", false, false],
    ["/login", false, true],
    [null, true, true],
  ] as const)(
    "offers recovery only when available (login=%s, bridge=%s)",
    async (loginUrl, bridge, available) => {
      vi.mocked(capabilities.getCachedServerInfo).mockReturnValue({
        ...capabilities.FALLBACK_SERVER_INFO,
        login_url: loginUrl,
      });
      vi.mocked(nativeBridge.supportsSignInAgain).mockReturnValue(bridge);
      render(<TerminalView sessionId="conv_abc" terminalId="terminal_bash_s1" />);
      await waitFor(() => expect(terminalSessionMock.instances).toHaveLength(1));
      act(() =>
        terminalSessionMock.instances[0].onState({
          kind: "closed",
          code: 4403,
          reason: "browser_session_missing",
        }),
      );
      expect(screen.queryByRole("button", { name: "Sign in again" }) !== null).toBe(available);
      expect(
        screen.getByText(
          available
            ? "Sign in again to use this terminal."
            : "Sign in again through this server's sign-in page to use this terminal.",
        ),
      ).toBeInTheDocument();
      expect(screen.queryByTestId("terminal-reconnecting")).not.toBeInTheDocument();
    },
  );

  it.each([
    ["origin_untrusted", "Open this terminal from a trusted server address."],
    ["runner_credential", "Sign in through the web app to use this terminal."],
    ["browser_session_missing", "Sign in again to use this terminal."],
    ["browser_account_mismatch", "Sign in with the session owner's account."],
    ["not_owner", "Only the session owner can control this terminal."],
    ["admin_not_allowed", "Administrator terminal access is not allowed."],
    ["access_denied", "Terminal access was denied."],
    ["toString", "Terminal access was denied."],
  ])("shows %s without reconnecting", async (reason, message) => {
    render(<TerminalView sessionId="conv_abc" terminalId="terminal_bash_s1" />);
    await waitFor(() => expect(terminalSessionMock.instances).toHaveLength(1));
    act(() => terminalSessionMock.instances[0].onState({ kind: "closed", code: 4403, reason }));
    expect(screen.getByText(message)).toBeInTheDocument();
    expect(screen.queryByTestId("terminal-reconnecting")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Sign in again" }) !== null).toBe(
      reason === "browser_session_missing",
    );
  });

  it("shows a cancelled sign-in and retries the terminal after successful sign-in", async () => {
    vi.mocked(signInForTerminal).mockResolvedValueOnce(false).mockResolvedValueOnce(true);
    render(<TerminalView sessionId="conv_abc" terminalId="terminal_bash_s1" />);
    await waitFor(() => expect(terminalSessionMock.instances).toHaveLength(1));
    act(() =>
      terminalSessionMock.instances[0].onState({
        kind: "closed",
        code: 4403,
        reason: "browser_session_missing",
      }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Sign in again" }));
    expect(await screen.findByText("Sign-in was not completed. Try again.")).toBeInTheDocument();
    expect(terminalSessionMock.instances).toHaveLength(1);
    fireEvent.click(screen.getByRole("button", { name: "Sign in again" }));
    await waitFor(() => expect(terminalSessionMock.instances).toHaveLength(2));
    expect(screen.getByText("Connecting…")).toBeInTheDocument();
  });

  it("waits for navigation without retrying an old cookie when sign-in redirects", async () => {
    vi.mocked(signInForTerminal).mockResolvedValue("redirecting");
    render(<TerminalView sessionId="conv_abc" terminalId="terminal_bash_s1" />);
    await waitFor(() => expect(terminalSessionMock.instances).toHaveLength(1));
    act(() =>
      terminalSessionMock.instances[0].onState({
        kind: "closed",
        code: 4403,
        reason: "browser_session_missing",
      }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Sign in again" }));
    expect(await screen.findByRole("button", { name: "Signing in…" })).toBeDisabled();
    expect(terminalSessionMock.instances).toHaveLength(1);
  });

  it("shows a failed sign-in beside an enabled retry action", async () => {
    vi.mocked(signInForTerminal).mockRejectedValue(new Error("sign-in transport failed"));
    render(<TerminalView sessionId="conv_abc" terminalId="terminal_bash_s1" />);
    await waitFor(() => expect(terminalSessionMock.instances).toHaveLength(1));
    act(() =>
      terminalSessionMock.instances[0].onState({
        kind: "closed",
        code: 4403,
        reason: "browser_session_missing",
      }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Sign in again" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Sign-in failed. Try again.");
    expect(screen.getByRole("button", { name: "Sign in again" })).toBeEnabled();
    expect(terminalSessionMock.instances).toHaveLength(1);
  });
});

vi.mock("./TerminalSession", async (importOriginal) => ({
  // Keep the real module (isUnexpectedTerminalClose and friends) —
  // only the session class itself is replaced.
  ...(await importOriginal<typeof TerminalSessionModule>()),
  TerminalSession: class {
    dispose = vi.fn();
    setTheme = vi.fn();
    setClipboardEnabled = vi.fn();
    sendSoftKey = vi.fn();
    cancelBarWrites = vi.fn();
    runBarKey = vi.fn((send: () => void) => send());
    stickyModifiers: TerminalStickyModifiers = { ctrl: "off", alt: "off", shift: "off" };
    setStickyModifiers = vi.fn((next: TerminalStickyModifiers) => {
      this.stickyModifiers = next;
    });
    getStickyModifiers = () => this.stickyModifiers;
    onStickyConsumed = vi.fn(() => vi.fn());
    focus = vi.fn();
    blur = vi.fn();
    isFocused = vi.fn(() => false);
    canPaste = vi.fn(() => true);
    pastesBracketed = vi.fn(() => true);
    paste = vi.fn(() => true);
    readHostClipboard = vi.fn();

    constructor(
      container: HTMLDivElement,
      url: string,
      onState: (state: ConnectionState) => void,
      _isDark?: boolean,
      onActivity?: () => void,
      _onInput?: () => void,
      clipboardEnabled = true,
      onClipboardRequest?: (text: string, copyEvent?: ClipboardEvent) => void,
      focusOnConnect = true,
      adaptCodexPalette = false,
      onFileLink?: (uri: string) => boolean,
      onClipboardAction?: TerminalClipboardAction,
    ) {
      terminalSessionMock.instances.push({
        url,
        container,
        clipboardEnabled,
        focusOnConnect,
        adaptCodexPalette,
        onClipboardRequest,
        onFileLink,
        onClipboardAction,
        canPaste: this.canPaste,
        pastesBracketed: this.pastesBracketed,
        paste: this.paste,
        readHostClipboard: this.readHostClipboard,
        onState,
        onActivity,
        dispose: this.dispose,
        setTheme: this.setTheme,
        setClipboardEnabled: this.setClipboardEnabled,
        sendSoftKey: this.sendSoftKey,
        getStickyModifiers: this.getStickyModifiers,
        focus: this.focus,
        blur: this.blur,
        isFocused: this.isFocused,
      });
    }
  },
}));

beforeEach(() => {
  localStorage.clear();
  act(() => toast.dismiss());
  render(<Toaster visibleToasts={100} />);
  terminalSessionMock.instances = [];
  clipboardMock.copyText.mockReset().mockResolvedValue(undefined);
  vi.mocked(resolveInitialAttachUrl).mockReset();
  vi.mocked(watchDirectUpgrade).mockReset();
});

afterEach(() => {
  act(() => toast.dismiss());
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllEnvs();
  setSessionHost("conv_abc", null);
});

describe("buildAttachPath", () => {
  it("addresses the terminal by resource id under /v1/sessions/.../resources/terminals", () => {
    expect(buildAttachPath("conv_abc", "terminal_bash_s1", false)).toBe(
      "/v1/sessions/conv_abc/resources/terminals/terminal_bash_s1/attach",
    );
  });

  it("omits ?read_only when the flag is false (common case)", () => {
    expect(buildAttachPath("conv_abc", "terminal_bash_s1", false).includes("?")).toBe(false);
  });

  it("appends ?read_only=true when requested", () => {
    expect(buildAttachPath("conv_abc", "terminal_bash_s1", true)).toBe(
      "/v1/sessions/conv_abc/resources/terminals/terminal_bash_s1/attach?read_only=true",
    );
  });

  it("appends ?omnigent_slice_key=host_id for host-sharded routing", () => {
    expect(buildAttachPath("conv_abc", "terminal_bash_s1", false, "host_123")).toBe(
      "/v1/sessions/conv_abc/resources/terminals/terminal_bash_s1/attach?omnigent_slice_key=host_123",
    );
  });

  it("appends ?await_launch=true for a terminal whose create is in flight", () => {
    expect(buildAttachPath("conv_abc", "terminal_bash_s1", false, undefined, true)).toBe(
      "/v1/sessions/conv_abc/resources/terminals/terminal_bash_s1/attach?await_launch=true",
    );
  });

  it("combines read_only and slice-key params", () => {
    const path = buildAttachPath("conv_abc", "terminal_bash_s1", true, "host_789");
    expect(path).toContain("read_only=true");
    expect(path).toContain("omnigent_slice_key=host_789");
  });

  it("omits ?omnigent_slice_key when no hostId is provided", () => {
    const path = buildAttachPath("conv_abc", "terminal_bash_s1", false);
    expect(path.includes("omnigent_slice_key")).toBe(false);
  });

  it("url-encodes the session and terminal ids", () => {
    const path = buildAttachPath("conv with space", "terminal/odd:id", false);
    expect(path).toContain("/v1/sessions/conv%20with%20space/");
    expect(path).toContain("/resources/terminals/terminal%2Fodd%3Aid/attach");
  });

  it("does not embed user-facing display names in the path", () => {
    // Resource-addressed routing was chosen specifically to keep
    // user-derived names (which can contain slashes / reserved
    // chars) out of the path. Pin that contract.
    const path = buildAttachPath("conv_abc", "terminal_bash_s1", false);
    expect(path).not.toContain("terminal_name=");
    expect(path).not.toContain("session_key=");
  });

  it("emits a path that starts at root (not a relative URL)", () => {
    // Caller composes the full URL with window.location.host;
    // a leading slash is required for that concatenation to be
    // correct against any page origin.
    expect(buildAttachPath("conv_abc", "terminal_bash_s1", false).startsWith("/")).toBe(true);
  });
});

describe("control-mode terminal", () => {
  it("attaches a host terminal through the shared relay client", async () => {
    const routing = vi.spyOn(host, "isHostRoutingEnabled").mockReturnValue(true);
    render(<TerminalView hostId="host-a" terminalId="aaaaaaaa" />);
    await waitFor(() => expect(terminalSessionMock.instances).toHaveLength(1));
    const url = new URL(terminalSessionMock.instances[0].url);
    expect(url.pathname).toBe("/v1/hosts/host-a/terminals/aaaaaaaa/attach");
    expect(url.searchParams.get("omnigent_slice_key")).toBe("host-a");
    expect(url.searchParams.has("read_only")).toBe(false);
    routing.mockRestore();
  });

  it("redials a host terminal without its slice key after wrong-replica 4400", async () => {
    const routing = vi.spyOn(host, "isHostRoutingEnabled").mockReturnValue(true);
    const databricks = vi.spyOn(host, "isDatabricksWorkspace").mockReturnValue(true);
    render(<TerminalView hostId="host-replica" terminalId="bbbbbbbb" />);
    await waitFor(() => expect(terminalSessionMock.instances).toHaveLength(1));
    act(() =>
      terminalSessionMock.instances[0].onState({
        kind: "closed",
        code: 4400,
        reason: "wrong_replica",
      }),
    );
    await waitFor(() => expect(terminalSessionMock.instances).toHaveLength(2));
    expect(
      new URL(terminalSessionMock.instances[1].url).searchParams.has("omnigent_slice_key"),
    ).toBe(false);
    expect(isHostKeyless("host-replica")).toBe(true);
    routing.mockRestore();
    databricks.mockRestore();
  });
  it.each([
    ["terminal_codex_main", true],
    ["terminal_claude_main", false],
    ["terminal_bash_s1", false],
  ])("adapts cached colors only for the Codex pane: %s", async (terminalId, expected) => {
    render(<TerminalView sessionId="conv_abc" terminalId={terminalId} readOnly />);
    await waitFor(() => expect(terminalSessionMock.instances).toHaveLength(1));
    expect(terminalSessionMock.instances[0].adaptCodexPalette).toBe(expected);
  });

  it("disables clipboard bridging for read-only attaches", async () => {
    render(<TerminalView sessionId="conv_abc" terminalId="terminal_bash_s1" readOnly />);
    await waitFor(() => expect(terminalSessionMock.instances).toHaveLength(1));
    expect(terminalSessionMock.instances[0].clipboardEnabled).toBe(false);
  });

  it("re-dials an attach refused during its launch once the create succeeds", async () => {
    vi.mocked(resolveInitialAttachUrl).mockImplementation(async (_direct, relay) => relay);
    setTerminalLaunching("terminal_zsh_u-retry1", "attach");
    render(<TerminalView sessionId="conv_abc" terminalId="terminal_zsh_u-retry1" />);
    await waitFor(() => expect(terminalSessionMock.instances).toHaveLength(1));

    // An older runner (or a launch past the server's wait) refuses the early attach.
    act(() =>
      terminalSessionMock.instances[0].onState({ kind: "closed", code: 4404, reason: "not found" }),
    );
    expect(screen.getByText("Starting shell…")).toBeTruthy();

    act(() => setTerminalLaunching("terminal_zsh_u-retry1", null));

    await waitFor(() => expect(terminalSessionMock.instances).toHaveLength(2));
    expect(terminalSessionMock.instances[1].url).not.toContain("await_launch");
  });

  it("re-dials an early attach that failed while the runner was being rebound", async () => {
    vi.mocked(resolveInitialAttachUrl).mockImplementation(async (_direct, relay) => relay);
    setTerminalLaunching("terminal_zsh_u-rebind1", "attach");
    render(<TerminalView sessionId="conv_abc" terminalId="terminal_zsh_u-rebind1" />);
    await waitFor(() => expect(terminalSessionMock.instances).toHaveLength(1));

    // No runner pinned yet: the relay closes with an internal error (4500).
    act(() =>
      terminalSessionMock.instances[0].onState({
        kind: "closed",
        code: 4500,
        reason: "runner attach factory failed",
      }),
    );
    expect(screen.getByText("Starting shell…")).toBeTruthy();
    act(() => setTerminalLaunching("terminal_zsh_u-rebind1", null));

    await waitFor(() => expect(terminalSessionMock.instances).toHaveLength(2));
    expect(terminalSessionMock.instances[1].url).not.toContain("await_launch");
  });

  it("re-dials when the refusal arrives after the create already succeeded", async () => {
    vi.mocked(resolveInitialAttachUrl).mockImplementation(async (_direct, relay) => relay);
    setTerminalLaunching("terminal_zsh_u-retry2", "attach");
    render(<TerminalView sessionId="conv_abc" terminalId="terminal_zsh_u-retry2" />);
    await waitFor(() => expect(terminalSessionMock.instances).toHaveLength(1));

    // The create's response lands first, then the early attach's 4404.
    act(() => setTerminalLaunching("terminal_zsh_u-retry2", null));
    act(() =>
      terminalSessionMock.instances[0].onState({ kind: "closed", code: 4404, reason: "not found" }),
    );

    await waitFor(() => expect(terminalSessionMock.instances).toHaveLength(2));
    expect(terminalSessionMock.instances[1].url).not.toContain("await_launch");
    // The re-dial is a plain attach: a second refusal stays closed.
    act(() =>
      terminalSessionMock.instances[1].onState({ kind: "closed", code: 4404, reason: "not found" }),
    );
    await act(async () => {});
    expect(terminalSessionMock.instances).toHaveLength(2);
  });

  it("keeps a 4404 outside a launch closed, and a live attach untouched when its launch ends", async () => {
    vi.mocked(resolveInitialAttachUrl).mockImplementation(async (_direct, relay) => relay);
    render(<TerminalView sessionId="conv_abc" terminalId="terminal_zsh_u-gone1" />);
    await waitFor(() => expect(terminalSessionMock.instances).toHaveLength(1));
    act(() =>
      terminalSessionMock.instances[0].onState({ kind: "closed", code: 4404, reason: "not found" }),
    );
    await act(async () => {});
    expect(terminalSessionMock.instances).toHaveLength(1);
    expect(screen.queryByText("Starting shell…")).toBeNull();
    cleanup();
    terminalSessionMock.instances = [];

    setTerminalLaunching("terminal_zsh_u-live1", "attach");
    render(<TerminalView sessionId="conv_abc" terminalId="terminal_zsh_u-live1" />);
    await waitFor(() => expect(terminalSessionMock.instances).toHaveLength(1));
    act(() => terminalSessionMock.instances[0].onState({ kind: "connected" }));
    act(() => setTerminalLaunching("terminal_zsh_u-live1", null));
    await act(async () => {});

    expect(terminalSessionMock.instances).toHaveLength(1);
    expect(terminalSessionMock.instances[0].dispose).not.toHaveBeenCalled();

    // A deliberate detach after the launch is not a launch refusal.
    act(() =>
      terminalSessionMock.instances[0].onState({ kind: "closed", code: 4405, reason: "detached" }),
    );
    await act(async () => {});
    expect(terminalSessionMock.instances).toHaveLength(1);
  });

  it("does not re-dial a launch-time attach that already reached its shell", async () => {
    vi.mocked(resolveInitialAttachUrl).mockImplementation(async (_direct, relay) => relay);
    setTerminalLaunching("terminal_zsh_u-used1", "attach");
    render(<TerminalView sessionId="conv_abc" terminalId="terminal_zsh_u-used1" />);
    await waitFor(() => expect(terminalSessionMock.instances).toHaveLength(1));
    act(() => terminalSessionMock.instances[0].onState({ kind: "connected" }));
    // Output arrived: the attach reached the live shell.
    act(() => terminalSessionMock.instances[0].onActivity?.());
    act(() => setTerminalLaunching("terminal_zsh_u-used1", null));

    act(() =>
      terminalSessionMock.instances[0].onState({ kind: "closed", code: 4500, reason: "bridge" }),
    );
    await act(async () => {});
    expect(terminalSessionMock.instances).toHaveLength(1);
  });

  it("holds a new shell's attach until its create returns when the runner may be waking", async () => {
    vi.mocked(resolveInitialAttachUrl).mockImplementation(async (_direct, relay) => relay);
    setTerminalLaunching("terminal_zsh_u-wait1", "wait");
    render(<TerminalView sessionId="conv_abc" terminalId="terminal_zsh_u-wait1" />);

    expect(screen.getByText("Starting shell…")).toBeTruthy();
    await act(async () => {});
    expect(terminalSessionMock.instances).toHaveLength(0);

    act(() => setTerminalLaunching("terminal_zsh_u-wait1", null));

    await waitFor(() => expect(terminalSessionMock.instances).toHaveLength(1));
    expect(terminalSessionMock.instances[0].url).not.toContain("await_launch");
  });

  it("attaches at once with await_launch when the runner is online", async () => {
    vi.mocked(resolveInitialAttachUrl).mockImplementation(async (_direct, relay) => relay);
    setTerminalLaunching("terminal_zsh_u-now1", "attach");
    try {
      render(<TerminalView sessionId="conv_abc" terminalId="terminal_zsh_u-now1" />);
      await waitFor(() => expect(terminalSessionMock.instances).toHaveLength(1));
      expect(terminalSessionMock.instances[0].url).toContain("await_launch=true");
    } finally {
      setTerminalLaunching("terminal_zsh_u-now1", null);
    }
  });

  it("does not render a legacy selection hint", async () => {
    render(<TerminalView sessionId="conv_abc" terminalId="terminal_bash_s1" />);
    await waitFor(() => expect(terminalSessionMock.instances).toHaveLength(1));
    expect(screen.queryByTestId("terminal-selection-hint")).toBeNull();
  });

  it("opens an OSC 8 workspace file citation in the FileViewer", async () => {
    const openFile = vi.fn();
    render(<FileViewerHarness openFile={openFile} />);
    await waitFor(() => expect(terminalSessionMock.instances).toHaveLength(1));

    expect(
      terminalSessionMock.instances[0].onFileLink?.("file:///home/u/ws/src/app.ts#L42-L50"),
    ).toBe(true);
    expect(openFile).toHaveBeenCalledWith("src/app.ts", { line: 42, endLine: 50 });
  });
});

describe("terminal clipboard", () => {
  async function renderClipboardView() {
    render(<TerminalView sessionId="conv_abc" terminalId="terminal_bash_s1" />);
    await waitFor(() => expect(terminalSessionMock.instances).toHaveLength(1));
    return terminalSessionMock.instances[0];
  }

  async function requestClipboard(
    text: string,
    terminalIndex = 0,
    copyEvent?: ClipboardEvent,
  ): Promise<void> {
    act(() => terminalSessionMock.instances[terminalIndex].onClipboardRequest?.(text, copyEvent));
    // Sonner publishes toast updates on the next event-loop turn.
    await act(async () => {
      await new Promise<void>((resolve) => {
        setTimeout(resolve, 0);
      });
    });
  }

  async function copySelection(text: string, terminalIndex = 0) {
    const setData = vi.fn();
    const event = new Event("copy", { bubbles: true, cancelable: true }) as ClipboardEvent;
    Object.defineProperty(event, "clipboardData", { value: { setData } });
    await requestClipboard(text, terminalIndex, event);
    return { event, setData };
  }

  function visibleClipboardConsent(): HTMLElement | null {
    return screen.queryByTestId("terminal-clipboard-consent");
  }

  function clickConsentButton(name: string): void {
    const prompt = visibleClipboardConsent();
    expect(prompt).not.toBeNull();
    fireEvent.click(within(prompt!).getByRole("button", { name }));
  }

  function uncheckRemember(): void {
    fireEvent.click(screen.getByRole("checkbox", { name: "Remember my choice" }));
  }

  async function allowForThisTerminal(): Promise<void> {
    uncheckRemember();
    clickConsentButton("Allow for this session");
    await waitFor(() => expect(visibleClipboardConsent()).toBeNull());
  }

  async function requestClipboardConsent(text: string, terminalIndex = 0): Promise<HTMLElement> {
    await requestClipboard(text, terminalIndex);
    await waitFor(() => expect(visibleClipboardConsent()).not.toBeNull());
    return visibleClipboardConsent()!;
  }

  it("requires consent before the first terminal clipboard write", async () => {
    const terminal = await renderClipboardView();

    const prompt = await requestClipboardConsent("copied text");

    expect(clipboardMock.copyText).not.toHaveBeenCalled();
    expect(prompt).not.toBeNull();
    expect(screen.getAllByText("Allow copying from terminals?").length).toBeGreaterThan(0);
    expect(prompt).toHaveTextContent("Your selection hasn’t been copied yet.");
    expect(prompt).toHaveTextContent(
      "Allowing copying also lets terminal programs silently replace your clipboard with text or commands you didn’t intend to paste.",
    );
    expect(within(prompt).getByRole("checkbox", { name: "Remember my choice" })).toBeChecked();
    expect(prompt).toHaveTextContent("Change this in Settings → General.");
    expect(prompt).not.toHaveTextContent("On this server, in this browser or app.");
    clickConsentButton("Copy once");
    await waitFor(() => expect(clipboardMock.copyText).toHaveBeenCalledWith("copied text"));
    await waitFor(() => expect(visibleClipboardConsent()).toBeNull());
    expect(terminal.focus).toHaveBeenCalled();
    expect(readTerminalClipboardPreference()).toBe("ask");

    await requestClipboardConsent("next text");
    expect(visibleClipboardConsent()).toBeInTheDocument();
    expect(clipboardMock.copyText).toHaveBeenCalledTimes(1);
  });

  it("copies an explicit selection under Ask, leaving program copies subject to consent", async () => {
    await renderClipboardView();
    const copied = await copySelection("browser selection");
    expect(copied.setData).toHaveBeenCalledWith("text/plain", "browser selection");
    expect(visibleClipboardConsent()).toBeNull();
    expect(clipboardMock.copyText).not.toHaveBeenCalled();
    expect(readTerminalClipboardPreference()).toBe("ask");
    await requestClipboardConsent("program copy");
  });

  it("uses the native copy event for allowed selections without requiring the async clipboard API", async () => {
    writeTerminalClipboardPreference("allow");
    await renderClipboardView();

    const copied = await copySelection("selected text");

    expect(copied.setData).toHaveBeenCalledWith("text/plain", "selected text");
    expect(copied.event.defaultPrevented).toBe(true);
    expect(clipboardMock.copyText).not.toHaveBeenCalled();
    expect(visibleClipboardConsent()).toBeNull();
  });

  it("blocks browser selection copies and explains how to re-enable them", async () => {
    writeTerminalClipboardPreference("block");
    await renderClipboardView();

    const copied = await copySelection("blocked text");

    expect(copied.setData).not.toHaveBeenCalled();
    expect(clipboardMock.copyText).not.toHaveBeenCalled();
    expect(visibleClipboardConsent()).toBeNull();
    expect(screen.getByText("Copying from this terminal is blocked.")).toBeInTheDocument();
    expect(screen.getByText("Change this in Settings → General.")).toBeInTheDocument();
  });

  it.each(["ask", "block"] as const)(
    "applies a change to %s to explicit selections immediately",
    async (decision) => {
      writeTerminalClipboardPreference("allow");
      await renderClipboardView();
      const before = await copySelection("allowed text");
      expect(before.setData).toHaveBeenCalledWith("text/plain", "allowed text");

      act(() => writeTerminalClipboardPreference(decision));
      const after = await copySelection("after revocation");

      if (decision === "ask")
        expect(after.setData).toHaveBeenCalledWith("text/plain", "after revocation");
      else expect(after.setData).not.toHaveBeenCalled();
      expect(clipboardMock.copyText).not.toHaveBeenCalled();
      expect(terminalSessionMock.instances).toHaveLength(1);
      expect(visibleClipboardConsent()).toBeNull();
    },
  );

  it("restores an explicit selection after an in-flight program copy completes", async () => {
    writeTerminalClipboardPreference("allow");
    await renderClipboardView();
    let finishOldCopy: (() => void) | undefined;
    clipboardMock.copyText.mockImplementationOnce(
      () =>
        new Promise<void>((resolve) => {
          finishOldCopy = resolve;
        }),
    );
    await requestClipboard("older program copy");
    const newer = await copySelection("newer browser selection");
    expect(newer.setData).toHaveBeenCalledWith("text/plain", "newer browser selection");
    expect(clipboardMock.copyText).toHaveBeenCalledTimes(1);
    await act(async () => finishOldCopy?.());
    expect(clipboardMock.copyText.mock.calls).toEqual([
      ["older program copy"],
      ["newer browser selection"],
    ]);
    expect(visibleClipboardConsent()).toBeNull();
  });

  it("lets a native selection copy satisfy a pending browser retry", async () => {
    writeTerminalClipboardPreference("allow");
    await renderClipboardView();
    clipboardMock.copyText.mockRejectedValueOnce(new Error("browser needs a gesture"));
    await requestClipboard("program selection");
    await waitFor(() => expect(visibleClipboardConsent()).toHaveTextContent("Copy needs a click"));

    const copied = await copySelection("explicit browser selection");

    expect(copied.setData).toHaveBeenCalledWith("text/plain", "explicit browser selection");
    await waitFor(() => expect(visibleClipboardConsent()).toBeNull());
    expect(clipboardMock.copyText).toHaveBeenCalledTimes(1);
    expect(readTerminalClipboardPreference()).toBe("allow");
  });

  it.each(["visibility", "preference", "terminal", "remount"] as const)(
    "serializes a newer selection behind an in-flight write across a %s change",
    async (transition) => {
      writeTerminalClipboardPreference("allow");
      const view = render(<TerminalView sessionId="conv_abc" terminalId="terminal_bash_s1" />);
      await waitFor(() => expect(terminalSessionMock.instances).toHaveLength(1));
      let clipboard = "before copying";
      let finishOldCopy: (() => void) | undefined;
      clipboardMock.copyText
        .mockImplementation(async (text) => {
          clipboard = text;
        })
        .mockImplementationOnce(
          (text) =>
            new Promise<void>((resolve) => {
              finishOldCopy = () => {
                clipboard = text;
                resolve();
              };
            }),
        );
      await requestClipboard("older program text");
      try {
        if (transition === "visibility") {
          view.rerender(
            <TerminalView sessionId="conv_abc" terminalId="terminal_bash_s1" active={false} />,
          );
          view.rerender(<TerminalView sessionId="conv_abc" terminalId="terminal_bash_s1" />);
        } else if (transition === "preference") {
          act(() => writeTerminalClipboardPreference("ask"));
          act(() => writeTerminalClipboardPreference("allow"));
        } else if (transition === "terminal") {
          view.rerender(<TerminalView sessionId="conv_next" terminalId="terminal_bash_s2" />);
          await waitFor(() => expect(terminalSessionMock.instances).toHaveLength(2));
        } else {
          view.unmount();
          render(<TerminalView sessionId="conv_next" terminalId="terminal_bash_s2" />);
          await waitFor(() => expect(terminalSessionMock.instances).toHaveLength(2));
        }
        const newer = await copySelection(
          "newer selection",
          terminalSessionMock.instances.length - 1,
        );
        expect(newer.setData).toHaveBeenCalledWith("text/plain", "newer selection");
        expect(clipboardMock.copyText).toHaveBeenCalledTimes(1);
        expect(clipboard).toBe("before copying");
      } finally {
        await act(async () => finishOldCopy?.());
      }
      expect(clipboardMock.copyText.mock.calls).toEqual([
        ["older program text"],
        ["newer selection"],
      ]);
      expect(clipboard).toBe("newer selection");
      expect(visibleClipboardConsent()).toBeNull();
    },
  );

  it.each(["ask", "allow", "block"] as const)(
    "honors %s for explicit selections in read-only terminals without allowing program writes",
    async (decision) => {
      writeTerminalClipboardPreference(decision);
      render(<TerminalView sessionId="conv_abc" terminalId="terminal_bash_s1" readOnly />);
      await waitFor(() => expect(terminalSessionMock.instances).toHaveLength(1));
      await requestClipboard("program request from read-only terminal");
      expect(clipboardMock.copyText).not.toHaveBeenCalled();
      expect(visibleClipboardConsent()).toBeNull();

      const copied = await copySelection("explicit read-only selection");

      if (decision === "block") expect(copied.setData).not.toHaveBeenCalled();
      else
        expect(copied.setData).toHaveBeenCalledWith("text/plain", "explicit read-only selection");
      expect(visibleClipboardConsent()).toBeNull();
      expect(clipboardMock.copyText).not.toHaveBeenCalled();
    },
  );

  it("does not copy selections from an inactive terminal", async () => {
    writeTerminalClipboardPreference("allow");
    render(<TerminalView sessionId="conv_abc" terminalId="terminal_bash_s1" active={false} />);
    await waitFor(() => expect(terminalSessionMock.instances).toHaveLength(1));

    const copied = await copySelection("hidden selection");

    expect(copied.setData).not.toHaveBeenCalled();
    expect(clipboardMock.copyText).not.toHaveBeenCalled();
    expect(visibleClipboardConsent()).toBeNull();
  });

  it("allows automatic copies for the rest of the mounted terminal session", async () => {
    await renderClipboardView();
    await requestClipboardConsent("first text");

    await allowForThisTerminal();
    await waitFor(() => expect(clipboardMock.copyText).toHaveBeenCalledWith("first text"));

    await requestClipboard("second text");
    await waitFor(() => expect(clipboardMock.copyText).toHaveBeenLastCalledWith("second text"));
    expect(clipboardMock.copyText).toHaveBeenCalledTimes(2);
    await waitFor(() => expect(visibleClipboardConsent()).toBeNull());
  });

  it.each(["allow", "block"] as const)(
    "remembers %s across conversations, terminals, and remounts",
    async (decision) => {
      const view = render(<TerminalView sessionId="conv_abc" terminalId="terminal_bash_s1" />);
      await waitFor(() => expect(terminalSessionMock.instances).toHaveLength(1));
      await requestClipboardConsent("first text");
      clickConsentButton(decision === "allow" ? "Allow copying" : "Block");

      expect(readTerminalClipboardPreference()).toBe(decision);
      await waitFor(() => expect(visibleClipboardConsent()).toBeNull());
      if (decision === "allow") {
        await waitFor(() => expect(clipboardMock.copyText).toHaveBeenCalledTimes(1));
        expect(clipboardMock.copyText).toHaveBeenLastCalledWith("first text");
      }

      view.rerender(<TerminalView sessionId="conv_next" terminalId="terminal_bash_s2" />);
      await waitFor(() => expect(terminalSessionMock.instances).toHaveLength(2));
      await requestClipboard("second text", 1);
      expect(visibleClipboardConsent()).toBeNull();
      if (decision === "allow") {
        await waitFor(() => expect(clipboardMock.copyText).toHaveBeenCalledTimes(2));
      }

      view.unmount();
      render(<TerminalView sessionId="conv_third" terminalId="terminal_bash_s3" />);
      await waitFor(() => expect(terminalSessionMock.instances).toHaveLength(3));
      await requestClipboard("third text", 2);
      expect(visibleClipboardConsent()).toBeNull();
      if (decision === "allow") {
        await waitFor(() => expect(clipboardMock.copyText).toHaveBeenCalledTimes(3));
        expect(clipboardMock.copyText).toHaveBeenLastCalledWith("third text");
      } else {
        expect(clipboardMock.copyText).not.toHaveBeenCalled();
      }
    },
  );

  it("updates an already-mounted terminal after another terminal remembers Allow", async () => {
    render(
      <>
        <TerminalView sessionId="conv_abc" terminalId="terminal_bash_s1" />
        <TerminalView sessionId="conv_next" terminalId="terminal_bash_s2" />
      </>,
    );
    await waitFor(() => expect(terminalSessionMock.instances).toHaveLength(2));
    await requestClipboardConsent("first text");
    clickConsentButton("Allow copying");
    await waitFor(() => expect(clipboardMock.copyText).toHaveBeenCalledTimes(1));

    await requestClipboard("other terminal", 1);
    await waitFor(() => expect(clipboardMock.copyText).toHaveBeenCalledTimes(2));
    expect(clipboardMock.copyText).toHaveBeenLastCalledWith("other terminal");
    await waitFor(() => expect(visibleClipboardConsent()).toBeNull());
  });

  it.each(["allow", "block"] as const)(
    "a remembered %s overrides an earlier opposite terminal-only choice",
    async (decision) => {
      render(
        <>
          <TerminalView sessionId="conv_abc" terminalId="terminal_bash_s1" />
          <TerminalView sessionId="conv_next" terminalId="terminal_bash_s2" />
        </>,
      );
      await waitFor(() => expect(terminalSessionMock.instances).toHaveLength(2));
      await requestClipboardConsent("terminal-only choice");
      uncheckRemember();
      clickConsentButton(decision === "allow" ? "Block" : "Allow for this session");
      await waitFor(() => expect(visibleClipboardConsent()).toBeNull());
      expect(readTerminalClipboardPreference()).toBe("ask");

      const before = await copySelection("before the remembered choice");
      if (decision === "allow") expect(before.setData).not.toHaveBeenCalled();
      else
        expect(before.setData).toHaveBeenCalledWith("text/plain", "before the remembered choice");

      await requestClipboardConsent("choice in another terminal", 1);
      expect(screen.getByRole("checkbox", { name: "Remember my choice" })).toBeChecked();
      clickConsentButton(decision === "allow" ? "Allow copying" : "Block");
      await waitFor(() => expect(visibleClipboardConsent()).toBeNull());
      expect(readTerminalClipboardPreference()).toBe(decision);
      clipboardMock.copyText.mockClear();

      const after = await copySelection("after the remembered choice");
      await requestClipboard("program copy after the remembered choice");

      if (decision === "allow") {
        expect(after.setData).toHaveBeenCalledWith("text/plain", "after the remembered choice");
        expect(clipboardMock.copyText).toHaveBeenCalledWith(
          "program copy after the remembered choice",
        );
      } else {
        expect(after.setData).not.toHaveBeenCalled();
        expect(clipboardMock.copyText).not.toHaveBeenCalled();
      }
      expect(visibleClipboardConsent()).toBeNull();
      expect(terminalSessionMock.instances).toHaveLength(2);
    },
  );

  it("keeps another terminal's pending program text available after remembered Allow", async () => {
    render(
      <>
        <TerminalView sessionId="conv_abc" terminalId="terminal_bash_s1" />
        <TerminalView sessionId="conv_next" terminalId="terminal_bash_s2" />
      </>,
    );
    await waitFor(() => expect(terminalSessionMock.instances).toHaveLength(2));
    const firstPrompt = await requestClipboardConsent("first terminal text");
    await requestClipboard("second terminal text", 1);
    await waitFor(() =>
      expect(screen.getAllByTestId("terminal-clipboard-consent")).toHaveLength(2),
    );
    const secondPrompt = screen
      .getAllByTestId("terminal-clipboard-consent")
      .find((prompt) => prompt !== firstPrompt)!;

    fireEvent.click(within(firstPrompt).getByRole("button", { name: "Allow copying" }));
    await waitFor(() => expect(firstPrompt).not.toBeInTheDocument());
    await waitFor(() => expect(secondPrompt).toHaveTextContent("Finish copying terminal text"));
    expect(secondPrompt).toHaveTextContent("The requested text hasn’t been copied yet.");
    expect(within(secondPrompt).queryByRole("checkbox")).not.toBeInTheDocument();
    expect(clipboardMock.copyText.mock.calls).toEqual([["first terminal text"]]);

    fireEvent.click(within(secondPrompt).getByRole("button", { name: "Copy now" }));
    await waitFor(() => expect(clipboardMock.copyText).toHaveBeenCalledTimes(2));
    expect(clipboardMock.copyText).toHaveBeenLastCalledWith("second terminal text");
    await waitFor(() => expect(secondPrompt).not.toBeInTheDocument());
    expect(readTerminalClipboardPreference()).toBe("allow");
  });

  it("replaces a pending shared-grant selection when the user copies newer text", async () => {
    await renderClipboardView();
    await requestClipboardConsent("older selection");
    act(() => writeTerminalClipboardPreference("allow"));
    await waitFor(() =>
      expect(visibleClipboardConsent()).toHaveTextContent("Finish copying terminal text"),
    );
    expect(clipboardMock.copyText).not.toHaveBeenCalled();

    await requestClipboard("newer selection");
    await waitFor(() => expect(visibleClipboardConsent()).toBeNull());
    expect(clipboardMock.copyText.mock.calls).toEqual([["newer selection"]]);
  });

  it.each(["ask", "block"] as const)(
    "discards a pending shared-grant selection after revocation to %s",
    async (decision) => {
      await renderClipboardView();
      await requestClipboardConsent("pending selection");
      act(() => writeTerminalClipboardPreference("allow"));
      await waitFor(() =>
        expect(visibleClipboardConsent()).toHaveTextContent("Finish copying terminal text"),
      );

      act(() => writeTerminalClipboardPreference(decision));
      await waitFor(() => expect(visibleClipboardConsent()).toBeNull());
      expect(clipboardMock.copyText).not.toHaveBeenCalled();
      await requestClipboard("selection after revocation");
      expect(clipboardMock.copyText).not.toHaveBeenCalled();
      if (decision === "ask") expect(visibleClipboardConsent()).toBeInTheDocument();
      else expect(visibleClipboardConsent()).toBeNull();
    },
  );

  it("keeps concurrent terminal prompts and their pending copies independent", async () => {
    render(
      <>
        <TerminalView sessionId="conv_abc" terminalId="terminal_bash_s1" />
        <TerminalView sessionId="conv_next" terminalId="terminal_bash_s2" />
      </>,
    );
    await waitFor(() => expect(terminalSessionMock.instances).toHaveLength(2));
    const firstPrompt = await requestClipboardConsent("first terminal text");
    fireEvent.click(within(firstPrompt).getByRole("checkbox"));

    await requestClipboard("second terminal text", 1);
    await waitFor(() =>
      expect(screen.getAllByTestId("terminal-clipboard-consent")).toHaveLength(2),
    );
    const secondPrompt = screen
      .getAllByTestId("terminal-clipboard-consent")
      .find((prompt) => prompt !== firstPrompt)!;
    expect(within(firstPrompt).getByRole("checkbox")).not.toBeChecked();
    expect(within(secondPrompt).getByRole("checkbox")).toBeChecked();
    expect(firstPrompt.closest("[data-sonner-toast]")).not.toBe(
      secondPrompt.closest("[data-sonner-toast]"),
    );

    await requestClipboard("newest first terminal text");
    expect(screen.getAllByTestId("terminal-clipboard-consent")).toHaveLength(2);
    expect(within(firstPrompt).getByRole("checkbox")).not.toBeChecked();
    expect(within(secondPrompt).getByRole("checkbox")).toBeChecked();
    expect(clipboardMock.copyText).not.toHaveBeenCalled();

    fireEvent.click(within(firstPrompt).getByRole("button", { name: "Allow for this session" }));
    await waitFor(() => expect(clipboardMock.copyText).toHaveBeenCalledTimes(1));
    expect(clipboardMock.copyText).toHaveBeenLastCalledWith("newest first terminal text");
    await waitFor(() => expect(firstPrompt).not.toBeInTheDocument());
    expect(secondPrompt).toBeInTheDocument();
    expect(within(secondPrompt).getByRole("checkbox")).toBeChecked();
    expect(readTerminalClipboardPreference()).toBe("ask");

    fireEvent.click(within(secondPrompt).getByRole("button", { name: "Copy once" }));
    await waitFor(() => expect(clipboardMock.copyText).toHaveBeenCalledTimes(2));
    expect(clipboardMock.copyText).toHaveBeenLastCalledWith("second terminal text");
    await waitFor(() => expect(secondPrompt).not.toBeInTheDocument());
    expect(readTerminalClipboardPreference()).toBe("ask");

    await requestClipboard("first terminal remains allowed");
    expect(clipboardMock.copyText).toHaveBeenCalledTimes(3);
    expect(clipboardMock.copyText).toHaveBeenLastCalledWith("first terminal remains allowed");
    await requestClipboard("second terminal still asks", 1);
    expect(visibleClipboardConsent()).toBeInTheDocument();
    expect(screen.getByRole("checkbox")).toBeChecked();
    expect(clipboardMock.copyText).toHaveBeenCalledTimes(3);
  });

  it("preserves an unchecked Remember choice when newer text replaces the request", async () => {
    const terminal = await renderClipboardView();
    const input = document.createElement("input");
    terminal.container.appendChild(input);
    input.focus();
    const prompt = await requestClipboardConsent("older text");

    expect(input).toHaveFocus();
    expect(screen.getByTestId("terminal-view")).not.toContainElement(prompt);
    expect(prompt.closest("[data-sonner-toaster]")).toHaveAttribute("data-x-position", "right");
    expect(prompt.closest("[data-sonner-toaster]")).toHaveAttribute("data-y-position", "bottom");
    expect(prompt).toHaveAttribute("role", "region");
    uncheckRemember();
    // Click before Sonner's asynchronous update publishes the new callback.
    act(() => terminal.onClipboardRequest?.("newest text"));
    expect(screen.getByRole("checkbox")).not.toBeChecked();
    clickConsentButton("Allow for this session");

    await waitFor(() => expect(clipboardMock.copyText).toHaveBeenCalledTimes(1));
    expect(clipboardMock.copyText).toHaveBeenCalledWith("newest text");
    expect(readTerminalClipboardPreference()).toBe("ask");
  });

  it("makes an exiting prompt inert while a different terminal asks for consent", async () => {
    const view = render(<TerminalView sessionId="conv_abc" terminalId="terminal_bash_s1" />);
    await waitFor(() => expect(terminalSessionMock.instances).toHaveLength(1));
    const oldPrompt = await requestClipboardConsent("old terminal text");
    const oldAllow = within(oldPrompt).getByRole("button", { name: "Allow copying" });
    const oldDismiss = within(oldPrompt).getByRole("button", { name: "Dismiss clipboard request" });

    view.rerender(<TerminalView sessionId="conv_abc" terminalId="terminal_bash_s2" />);
    await waitFor(() => expect(terminalSessionMock.instances).toHaveLength(2));
    await requestClipboard("new terminal text", 1);
    await waitFor(() =>
      expect(screen.getAllByTestId("terminal-clipboard-consent")).toHaveLength(2),
    );
    const newPrompt = screen
      .getAllByTestId("terminal-clipboard-consent")
      .find((prompt) => prompt !== oldPrompt)!;

    // Sonner keeps the old controls in the DOM during their exit animation.
    expect(oldPrompt).toBeInTheDocument();
    fireEvent.click(oldAllow);
    fireEvent.click(oldDismiss);
    expect(clipboardMock.copyText).not.toHaveBeenCalled();
    expect(readTerminalClipboardPreference()).toBe("ask");
    expect(newPrompt).toBeInTheDocument();

    fireEvent.click(within(newPrompt).getByRole("button", { name: "Copy once" }));
    await waitFor(() => expect(clipboardMock.copyText).toHaveBeenCalledTimes(1));
    expect(clipboardMock.copyText).toHaveBeenCalledWith("new terminal text");
    expect(readTerminalClipboardPreference()).toBe("ask");
    await waitFor(() =>
      expect(screen.queryAllByTestId("terminal-clipboard-consent")).toHaveLength(0),
    );
  });

  it("dismisses without granting permission or remembering a choice", async () => {
    await renderClipboardView();
    await requestClipboardConsent("dismissed text");
    clickConsentButton("Dismiss clipboard request");

    await waitFor(() => expect(visibleClipboardConsent()).toBeNull());
    expect(clipboardMock.copyText).not.toHaveBeenCalled();
    expect(readTerminalClipboardPreference()).toBe("ask");
    await requestClipboardConsent("new text");
    expect(screen.getByRole("checkbox")).toBeChecked();
  });

  it("reports failed persistence and grants only the mounted terminal", async () => {
    const view = render(<TerminalView sessionId="conv_abc" terminalId="terminal_bash_s1" />);
    await waitFor(() => expect(terminalSessionMock.instances).toHaveLength(1));
    await requestClipboardConsent("first text");
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("storage denied");
    });
    clickConsentButton("Allow copying");
    await waitFor(() => expect(clipboardMock.copyText).toHaveBeenCalledTimes(1));
    expect(readTerminalClipboardPreference()).toBe("ask");
    expect(await screen.findByText(/Couldn't remember your clipboard choice/)).toBeInTheDocument();

    await requestClipboard("second text");
    await waitFor(() => expect(clipboardMock.copyText).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(visibleClipboardConsent()).toBeNull());
    view.unmount();
    render(<TerminalView sessionId="conv_abc" terminalId="terminal_bash_s1" />);
    await waitFor(() => expect(terminalSessionMock.instances).toHaveLength(2));
    await requestClipboard("after remount", 1);
    expect(visibleClipboardConsent()).toBeInTheDocument();
    expect(clipboardMock.copyText).toHaveBeenCalledTimes(2);
  });

  it.each(["allow", "block"] as const)(
    "offers only terminal-scoped %s when the server identity is unavailable",
    async (decision) => {
      vi.spyOn(host, "getOmnigentServerIdentity").mockReturnValue(null);
      const view = render(<TerminalView sessionId="conv_abc" terminalId="terminal_bash_s1" />);
      await waitFor(() => expect(terminalSessionMock.instances).toHaveLength(1));
      const prompt = await requestClipboardConsent("first selection");
      const remember = within(prompt).getByRole("checkbox", { name: "Remember my choice" });
      expect(remember).toBeDisabled();
      expect(remember).not.toBeChecked();
      expect(prompt).toHaveTextContent("This connection can’t remember clipboard permissions.");

      clickConsentButton(decision === "allow" ? "Allow for this session" : "Block");
      await waitFor(() => expect(visibleClipboardConsent()).toBeNull());
      expect(screen.queryByText(/Couldn't remember your clipboard choice/)).not.toBeInTheDocument();
      expect(readTerminalClipboardPreference()).toBe("ask");
      await requestClipboard("second selection");
      expect(clipboardMock.copyText).toHaveBeenCalledTimes(decision === "allow" ? 2 : 0);
      expect(visibleClipboardConsent()).toBeNull();

      view.unmount();
      render(<TerminalView sessionId="conv_abc" terminalId="terminal_bash_s1" />);
      await waitFor(() => expect(terminalSessionMock.instances).toHaveLength(2));
      await requestClipboardConsent("selection after remount", 1);
      expect(clipboardMock.copyText).toHaveBeenCalledTimes(decision === "allow" ? 2 : 0);
    },
  );

  it("does not retain a grant when the mounted view changes servers", async () => {
    const identity = vi.spyOn(host, "getOmnigentServerIdentity").mockReturnValue("server-one");
    writeTerminalClipboardPreference("allow");
    const view = render(<TerminalView sessionId="conv_abc" terminalId="terminal_bash_s1" />);
    await waitFor(() => expect(terminalSessionMock.instances).toHaveLength(1));
    await requestClipboard("trusted server");
    await waitFor(() => expect(clipboardMock.copyText).toHaveBeenCalledTimes(1));

    identity.mockReturnValue("server-two");
    view.rerender(<TerminalView sessionId="conv_abc" terminalId="terminal_bash_s1" />);
    await waitFor(() => expect(terminalSessionMock.instances).toHaveLength(2));
    await requestClipboard("stale server callback");
    expect(visibleClipboardConsent()).toBeNull();
    await requestClipboard("untrusted server", 1);
    expect(visibleClipboardConsent()).toBeInTheDocument();
    expect(clipboardMock.copyText).toHaveBeenCalledTimes(1);
    expect(readTerminalClipboardPreference()).toBe("ask");
  });

  it.each([{ active: false }, { readOnly: true }])(
    "does not let remembered Allow bypass inactive or read-only guards: %j",
    async (props) => {
      writeTerminalClipboardPreference("allow");
      render(<TerminalView sessionId="conv_abc" terminalId="terminal_bash_s1" {...props} />);
      await waitFor(() => expect(terminalSessionMock.instances).toHaveLength(1));
      await requestClipboard("not eligible");

      expect(clipboardMock.copyText).not.toHaveBeenCalled();
      expect(visibleClipboardConsent()).toBeNull();
    },
  );

  it.each(["ask", "block"] as const)(
    "revokes mounted consent and queued copies when Settings changes to %s",
    async (decision) => {
      await renderClipboardView();
      await requestClipboardConsent("initial text");
      await allowForThisTerminal();
      await waitFor(() => expect(clipboardMock.copyText).toHaveBeenCalledTimes(1));

      let rejectInFlight: ((reason: Error) => void) | undefined;
      clipboardMock.copyText.mockImplementationOnce(
        () =>
          new Promise<void>((_resolve, reject) => {
            rejectInFlight = reject;
          }),
      );
      await requestClipboard("in flight");
      await requestClipboard("queued before revocation");
      act(() => writeTerminalClipboardPreference(decision));
      await act(async () => rejectInFlight?.(new Error("revoked")));

      expect(clipboardMock.copyText).toHaveBeenCalledTimes(2);
      expect(visibleClipboardConsent()).toBeNull();
      await requestClipboard("after revocation");
      expect(clipboardMock.copyText).toHaveBeenCalledTimes(2);
      if (decision === "ask") expect(visibleClipboardConsent()).toBeInTheDocument();
      else expect(visibleClipboardConsent()).toBeNull();
    },
  );

  it("revokes a remembered grant when another tab clears it", async () => {
    writeTerminalClipboardPreference("allow");
    await renderClipboardView();
    await requestClipboard("allowed");
    await waitFor(() => expect(clipboardMock.copyText).toHaveBeenCalledTimes(1));

    act(() => {
      localStorage.clear();
      window.dispatchEvent(new StorageEvent("storage", { key: null, storageArea: localStorage }));
    });
    await requestClipboardConsent("after reset in another tab");
    expect(clipboardMock.copyText).toHaveBeenCalledTimes(1);
  });

  it("resets clipboard consent when the view switches terminals", async () => {
    const { rerender } = render(
      <TerminalView sessionId="conv_abc" terminalId="terminal_bash_s1" />,
    );
    await waitFor(() => expect(terminalSessionMock.instances).toHaveLength(1));
    await requestClipboardConsent("first terminal");
    await allowForThisTerminal();
    await waitFor(() => expect(clipboardMock.copyText).toHaveBeenCalledTimes(1));

    let rejectOldCopy: ((reason: Error) => void) | undefined;
    clipboardMock.copyText.mockImplementationOnce(
      () =>
        new Promise<void>((_resolve, reject) => {
          rejectOldCopy = reject;
        }),
    );
    await requestClipboard("pending old terminal");
    await waitFor(() => expect(clipboardMock.copyText).toHaveBeenCalledTimes(2));
    act(() => toast.dismiss());

    rerender(<TerminalView sessionId="conv_abc" terminalId="terminal_bash_s2" />);
    await waitFor(() => expect(terminalSessionMock.instances).toHaveLength(2));
    await act(async () => rejectOldCopy?.(new Error("old terminal closed")));
    await waitFor(() => expect(visibleClipboardConsent()).toBeNull());
    expect(screen.queryByText("Couldn't copy terminal selection to the clipboard.")).toBeNull();

    await requestClipboard("second terminal", 1);
    await waitFor(() => expect(visibleClipboardConsent()).not.toBeNull());
    expect(visibleClipboardConsent()).toBeInTheDocument();
    expect(clipboardMock.copyText).toHaveBeenCalledTimes(2);
  });

  it("discards an unanswered prompt while the terminal is hidden", async () => {
    const { rerender } = render(
      <TerminalView sessionId="conv_abc" terminalId="terminal_bash_s1" />,
    );
    await waitFor(() => expect(terminalSessionMock.instances).toHaveLength(1));
    await requestClipboardConsent("stale text");
    expect(visibleClipboardConsent()).toBeInTheDocument();

    rerender(<TerminalView sessionId="conv_abc" terminalId="terminal_bash_s1" active={false} />);
    rerender(<TerminalView sessionId="conv_abc" terminalId="terminal_bash_s1" active />);
    await waitFor(() => expect(visibleClipboardConsent()).toBeNull());

    await requestClipboardConsent("fresh text");
    expect(visibleClipboardConsent()).toBeInTheDocument();
  });

  it("resets clipboard consent after a read-only permission transition", async () => {
    const { rerender } = render(
      <TerminalView sessionId="conv_abc" terminalId="terminal_bash_s1" />,
    );
    await waitFor(() => expect(terminalSessionMock.instances).toHaveLength(1));
    await requestClipboardConsent("initial text");
    await allowForThisTerminal();
    await waitFor(() => expect(clipboardMock.copyText).toHaveBeenCalledTimes(1));

    rerender(<TerminalView sessionId="conv_abc" terminalId="terminal_bash_s1" readOnly />);
    await waitFor(() => expect(terminalSessionMock.instances).toHaveLength(2));
    rerender(<TerminalView sessionId="conv_abc" terminalId="terminal_bash_s1" />);
    await waitFor(() => expect(terminalSessionMock.instances).toHaveLength(3));
    await requestClipboard("after read-only", 2);
    await waitFor(() => expect(visibleClipboardConsent()).not.toBeNull());

    expect(visibleClipboardConsent()).toBeInTheDocument();
    expect(clipboardMock.copyText).toHaveBeenCalledTimes(1);
  });

  it("suppresses stale clipboard completion after the terminal unmounts", async () => {
    const { unmount } = render(<TerminalView sessionId="conv_abc" terminalId="terminal_bash_s1" />);
    await waitFor(() => expect(terminalSessionMock.instances).toHaveLength(1));
    await requestClipboardConsent("initial text");
    await allowForThisTerminal();
    await waitFor(() => expect(clipboardMock.copyText).toHaveBeenCalledTimes(1));

    let rejectInFlight: ((reason: Error) => void) | undefined;
    clipboardMock.copyText.mockImplementationOnce(
      () =>
        new Promise<void>((_resolve, reject) => {
          rejectInFlight = reject;
        }),
    );
    await requestClipboard("pending text");
    await waitFor(() => expect(clipboardMock.copyText).toHaveBeenCalledTimes(2));
    act(() => toast.dismiss());
    const terminal = terminalSessionMock.instances[0];

    unmount();
    await act(async () => rejectInFlight?.(new Error("unmounted")));

    expect(screen.queryByText("Couldn't copy terminal selection to the clipboard.")).toBeNull();
    expect(terminal.focus).toHaveBeenCalledTimes(1);
  });

  it("coalesces session-approved automatic copies to the newest pending text", async () => {
    await renderClipboardView();
    await requestClipboardConsent("initial text");
    await allowForThisTerminal();
    await waitFor(() => expect(clipboardMock.copyText).toHaveBeenCalledTimes(1));

    let resolveInFlight: (() => void) | undefined;
    clipboardMock.copyText.mockImplementationOnce(
      () =>
        new Promise<void>((resolve) => {
          resolveInFlight = resolve;
        }),
    );
    await requestClipboard("superseded text");
    await requestClipboard("newest text");
    await waitFor(() => expect(clipboardMock.copyText).toHaveBeenCalledTimes(2));
    expect(clipboardMock.copyText).toHaveBeenLastCalledWith("superseded text");

    await act(async () => resolveInFlight?.());
    await waitFor(() => expect(clipboardMock.copyText).toHaveBeenCalledTimes(3));
    expect(clipboardMock.copyText).toHaveBeenLastCalledWith("newest text");
  });

  it("blocks clipboard requests for the rest of the mounted terminal session", async () => {
    await renderClipboardView();
    await requestClipboardConsent("blocked text");

    uncheckRemember();
    clickConsentButton("Block");
    await requestClipboard("still blocked");

    expect(clipboardMock.copyText).not.toHaveBeenCalled();
    expect(readTerminalClipboardPreference()).toBe("ask");
    await waitFor(() => expect(visibleClipboardConsent()).toBeNull());
    expect(screen.getByText("Copying from this terminal is blocked.")).toBeInTheDocument();
    await requestClipboard("another blocked native copy");
    expect(screen.getAllByText("Copying from this terminal is blocked.")).toHaveLength(1);
  });

  it("keeps remembered consent when the browser requires a click to copy", async () => {
    await renderClipboardView();
    await requestClipboardConsent("first text");
    clickConsentButton("Allow copying");
    await waitFor(() => expect(clipboardMock.copyText).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(visibleClipboardConsent()).toBeNull());

    clipboardMock.copyText
      .mockRejectedValueOnce(new Error("permission denied"))
      .mockResolvedValueOnce(undefined);
    act(() => toast.dismiss());
    await requestClipboard("retry text");

    await waitFor(() => expect(visibleClipboardConsent()).toHaveTextContent("Copy needs a click"));
    expect(screen.queryByRole("checkbox")).toBeNull();
    expect(readTerminalClipboardPreference()).toBe("allow");
    clickConsentButton("Copy now");

    await waitFor(() => expect(clipboardMock.copyText).toHaveBeenCalledTimes(3));
    expect(clipboardMock.copyText).toHaveBeenLastCalledWith("retry text");
    expect(readTerminalClipboardPreference()).toBe("allow");
    await waitFor(() => expect(visibleClipboardConsent()).toBeNull());
  });

  it("keeps failed retries visible without retrying unsolicited writes", async () => {
    writeTerminalClipboardPreference("allow");
    await renderClipboardView();
    clipboardMock.copyText.mockRejectedValue(new Error("browser denied"));
    await requestClipboardConsent("first attempt");
    expect(visibleClipboardConsent()).toHaveTextContent("Copy needs a click");

    await requestClipboard("newer selection");
    expect(clipboardMock.copyText).toHaveBeenCalledTimes(1);
    clickConsentButton("Copy now");
    await waitFor(() =>
      expect(visibleClipboardConsent()).toHaveTextContent(
        "Check your browser’s clipboard permissions",
      ),
    );
    expect(clipboardMock.copyText).toHaveBeenLastCalledWith("newer selection");
    expect(readTerminalClipboardPreference()).toBe("allow");

    clickConsentButton("Dismiss");
    await waitFor(() => expect(visibleClipboardConsent()).toBeNull());
    await requestClipboard("next selection");
    expect(clipboardMock.copyText).toHaveBeenCalledTimes(2);
    expect(visibleClipboardConsent()).toHaveTextContent("Copy needs a click");
  });
});

describe("hidden pre-warmed surface", () => {
  it("keeps one live session across an active flip and focuses on reveal", async () => {
    // Mount hidden (a pre-warmed attach behind the chat view): the
    // session dials immediately — that is the whole point of the
    // pre-warm — but must not take focus away from the composer.
    const { rerender } = render(
      <TerminalView sessionId="conv_abc" terminalId="terminal_bash_s1" active={false} />,
    );
    await waitFor(() => expect(terminalSessionMock.instances).toHaveLength(1));
    const inst = terminalSessionMock.instances[0];
    expect(inst.focus).not.toHaveBeenCalled();

    // Reveal: the SAME session is kept (no re-dial — a second instance
    // here means the flip reconnected the WebSocket) and focused, since
    // the WS-open auto-focus was a no-op while hidden.
    rerender(<TerminalView sessionId="conv_abc" terminalId="terminal_bash_s1" active />);
    await waitFor(() => expect(inst.focus).toHaveBeenCalledTimes(1));
    expect(terminalSessionMock.instances).toHaveLength(1);
    expect(inst.dispose).not.toHaveBeenCalled();

    // Hiding again neither disposes nor re-focuses.
    rerender(<TerminalView sessionId="conv_abc" terminalId="terminal_bash_s1" active={false} />);
    expect(inst.dispose).not.toHaveBeenCalled();
    expect(inst.focus).toHaveBeenCalledTimes(1);
  });

  it("toggles clipboard bridging when a warm surface is revealed", async () => {
    const { rerender } = render(
      <TerminalView sessionId="conv_abc" terminalId="terminal_bash_s1" active={false} />,
    );
    await waitFor(() => expect(terminalSessionMock.instances).toHaveLength(1));
    const inst = terminalSessionMock.instances[0];
    expect(inst.clipboardEnabled).toBe(false);

    rerender(<TerminalView sessionId="conv_abc" terminalId="terminal_bash_s1" active />);
    await waitFor(() => expect(inst.setClipboardEnabled).toHaveBeenCalledWith(true));
    expect(terminalSessionMock.instances).toHaveLength(1);
  });

  it("does not focus on a plain active mount (WS-open handles it)", async () => {
    render(<TerminalView sessionId="conv_abc" terminalId="terminal_bash_s1" />);
    await waitFor(() => expect(terminalSessionMock.instances).toHaveLength(1));
    // No reveal edge — the session's own WS-open focus owns this case;
    // an extra explicit call would steal focus on every reconnect.
    expect(terminalSessionMock.instances[0].focus).not.toHaveBeenCalled();
  });
});

describe("unmount", () => {
  const DIRECT_URL =
    "ws://127.0.0.1:54321/v1/sessions/conv_abc/resources/terminals/terminal_bash_s1/attach?token=t";

  it("disposes the live session so its WebSocket closes", async () => {
    const onStateChange = vi.fn();
    const { unmount } = render(
      <TerminalView
        sessionId="conv_abc"
        terminalId="terminal_bash_s1"
        onStateChange={onStateChange}
      />,
    );
    await waitFor(() => expect(terminalSessionMock.instances).toHaveLength(1));
    unmount();
    await act(async () => {});

    expect(terminalSessionMock.instances[0].dispose).toHaveBeenCalledTimes(1);
    expect(onStateChange).toHaveBeenLastCalledWith(null);
  });

  it.each([
    ["plain", (el: ReactElement) => el],
    ["StrictMode", (el: ReactElement) => <StrictMode>{el}</StrictMode>],
  ])("lets a same-id successor's state win over the outgoing null (%s)", async (_name, wrap) => {
    const onStateChange = vi.fn();
    const view = (conv: string) =>
      wrap(
        <div key={`${conv}:terminal_claude_main`}>
          <TerminalView
            sessionId={conv}
            terminalId="terminal_claude_main"
            onStateChange={onStateChange}
          />
        </div>,
      );
    const { rerender } = render(view("conv_a"));
    await act(async () => {});
    expect(onStateChange).toHaveBeenLastCalledWith({ kind: "connecting" });
    rerender(view("conv_b"));
    await act(async () => {});

    expect(onStateChange).toHaveBeenLastCalledWith({ kind: "connecting" });
  });

  it("does not dial when it unmounts while the initial URL is resolving", async () => {
    let resolveUrl: ((url: string) => void) | undefined;
    vi.mocked(resolveInitialAttachUrl).mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveUrl = resolve;
        }),
    );
    const { unmount } = render(
      <TerminalView
        sessionId="conv_abc"
        terminalId="terminal_bash_s1"
        directAttachUrl={DIRECT_URL}
      />,
    );
    await waitFor(() => expect(resolveUrl).toBeDefined());
    unmount();
    await act(async () => resolveUrl!(DIRECT_URL));

    expect(terminalSessionMock.instances).toHaveLength(0);
  });

  it("keeps the upgrade watch live under StrictMode and aborts it on unmount", async () => {
    let upgradeSignal: AbortSignal | undefined;
    vi.mocked(resolveInitialAttachUrl).mockImplementationOnce(async (_direct, relay) => relay);
    vi.mocked(watchDirectUpgrade).mockImplementationOnce((_direct, signal) => {
      upgradeSignal = signal;
      return new Promise(() => {});
    });
    const { unmount } = render(
      <StrictMode>
        <TerminalView
          sessionId="conv_abc"
          terminalId="terminal_bash_s1"
          directAttachUrl={DIRECT_URL}
        />
      </StrictMode>,
    );
    await waitFor(() => expect(upgradeSignal).toBeDefined());
    expect(upgradeSignal!.aborted).toBe(false);
    unmount();
    await act(async () => {});

    expect(upgradeSignal!.aborted).toBe(true);
  });

  it("does not dial when it unmounts before the deferred attach runs", async () => {
    const { unmount } = render(<TerminalView sessionId="conv_abc" terminalId="terminal_bash_s1" />);
    unmount();
    await act(async () => {});

    expect(terminalSessionMock.instances).toHaveLength(0);
  });

  it("dials exactly once under StrictMode and keeps that session live", async () => {
    render(
      <StrictMode>
        <TerminalView sessionId="conv_abc" terminalId="terminal_bash_s1" />
      </StrictMode>,
    );
    await act(async () => {});

    expect(terminalSessionMock.instances).toHaveLength(1);
    expect(terminalSessionMock.instances[0].dispose).not.toHaveBeenCalled();
  });
});

describe("late direct-attach advert", () => {
  it("retires the outgoing session when the advert lands on a live terminal", async () => {
    // The runner's loopback advert reaches the client on a terminals
    // refetch — after the terminal has already dialed. That prop change
    // re-runs the attach ref for the SAME mount node (React 18 hands the
    // node back rather than remounting, and skips the ref's cleanup), so
    // the attach itself has to retire its predecessor. Without that,
    // xterm stacks a second instance inside one node — two helper
    // textareas, two renderers, two live bridges.
    const { rerender } = render(
      <TerminalView sessionId="conv_abc" terminalId="terminal_bash_s1" />,
    );
    await act(async () => {});
    expect(terminalSessionMock.instances).toHaveLength(1);
    const relayed = terminalSessionMock.instances[0];

    rerender(
      <TerminalView
        sessionId="conv_abc"
        terminalId="terminal_bash_s1"
        directAttachUrl={
          "ws://127.0.0.1:54321/v1/sessions/conv_abc" +
          "/resources/terminals/terminal_bash_s1/attach?token=t"
        }
      />,
    );
    await act(async () => {});

    // Same node, so this is a re-attach rather than a remount — which is
    // exactly why the predecessor cannot be left running.
    const readvertised = terminalSessionMock.instances.at(-1)!;
    expect(readvertised).not.toBe(relayed);
    expect(readvertised.container).toBe(relayed.container);
    expect(relayed.dispose).toHaveBeenCalled();
  });
});

describe("closed bridge overlay", () => {
  it.each<ConnectionState>([
    { kind: "connecting" },
    { kind: "closed", reason: "not found", code: 4404 },
    { kind: "error" },
  ])(
    "redials a $kind bridge after external recovery and preserves an already connected pane",
    async (state) => {
      const props = { sessionId: "conv_abc", terminalId: "terminal_codex_main" };
      const { rerender } = render(<TerminalView {...props} resumeGeneration={0} />);
      await waitFor(() => expect(terminalSessionMock.instances).toHaveLength(1));
      const previous = terminalSessionMock.instances[0];
      act(() => previous.onState(state));

      rerender(<TerminalView {...props} resumeGeneration={1} />);
      await waitFor(() => expect(terminalSessionMock.instances).toHaveLength(2));
      expect(previous.dispose).toHaveBeenCalledTimes(1);
      const connected = terminalSessionMock.instances[1];
      act(() => connected.onState({ kind: "connected" }));
      rerender(<TerminalView {...props} resumeGeneration={2} />);
      await act(async () => {});
      expect(terminalSessionMock.instances).toHaveLength(2);
      expect(connected.dispose).not.toHaveBeenCalled();
    },
  );

  it("redials once when its resume callback also signals successful external recovery", async () => {
    const onResume = vi.fn().mockResolvedValue(undefined);
    const props = { sessionId: "conv_abc", terminalId: "terminal_codex_main", onResume };
    const { rerender } = render(<TerminalView {...props} resumeGeneration={0} />);
    await waitFor(() => expect(terminalSessionMock.instances).toHaveLength(1));
    act(() => {
      terminalSessionMock.instances[0].onState({ kind: "closed", reason: "stopped", code: 4404 });
    });

    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Resume session" })));
    expect(onResume).toHaveBeenCalledTimes(1);
    expect(terminalSessionMock.instances).toHaveLength(1);
    rerender(<TerminalView {...props} resumeGeneration={1} />);
    await waitFor(() => expect(terminalSessionMock.instances).toHaveLength(2));
  });

  it("renders a resume button beside the closed message and invokes the callback", async () => {
    const onResume = vi.fn().mockResolvedValue(undefined);
    render(<TerminalView sessionId="conv_abc" terminalId="terminal_bash_s1" onResume={onResume} />);
    // One initial instance means the bridge mounted exactly once before the
    // closed state; zero would mean no terminal attached, two would mean a
    // duplicate WebSocket handshake before resume.
    await waitFor(() => expect(terminalSessionMock.instances).toHaveLength(1));

    act(() => {
      terminalSessionMock.instances[0].onState({ kind: "closed", reason: "stopped", code: 4405 });
    });

    expect(screen.getByText("Bridge closed: stopped")).toBeInTheDocument();
    const button = screen.getByRole("button", { name: /^resume session$/i });
    expect(button).toBeEnabled();

    fireEvent.click(button);
    // Exactly one resume call proves the button is wired once; zero would
    // mean it is inert, while multiple calls would duplicate the server
    // relaunch request.
    await waitFor(() => expect(onResume).toHaveBeenCalledTimes(1));
    // One instance is the initial bridge; a second appears only after
    // successful resume, proving the xterm mount remounted to reconnect.
    await waitFor(() => expect(terminalSessionMock.instances).toHaveLength(2));
  });

  it("disables the resume button while resume is pending", async () => {
    render(
      <TerminalView
        sessionId="conv_abc"
        terminalId="terminal_bash_s1"
        onResume={vi.fn()}
        resumePending
      />,
    );
    // The pending-state assertion must run against the first bridge mount;
    // extra instances here would mean props alone caused an unwanted remount.
    await waitFor(() => expect(terminalSessionMock.instances).toHaveLength(1));

    act(() => {
      terminalSessionMock.instances[0].onState({ kind: "closed", reason: "stopped", code: 4405 });
    });

    expect(screen.getByRole("button", { name: /^resuming/i })).toBeDisabled();
  });

  it("does not render a resume button when no action is provided", async () => {
    render(<TerminalView sessionId="conv_abc" terminalId="terminal_bash_s1" />);
    // Without an onResume prop the terminal still mounts once, but the closed
    // overlay must not invent its own resume action.
    await waitFor(() => expect(terminalSessionMock.instances).toHaveLength(1));

    act(() => {
      terminalSessionMock.instances[0].onState({ kind: "closed", reason: "stopped", code: 4405 });
    });

    expect(screen.queryByRole("button", { name: /^resume session$/i })).toBeNull();
  });

  it("keeps the closed overlay visible and surfaces resume failures", async () => {
    const onResume = vi.fn().mockRejectedValue(new Error("host offline"));
    render(
      <TerminalView
        sessionId="conv_abc"
        terminalId="terminal_bash_s1"
        onResume={onResume}
        resumeGeneration={0}
      />,
    );
    // Start from exactly one bridge so the later length check proves failed
    // resume did not remount xterm.
    await waitFor(() => expect(terminalSessionMock.instances).toHaveLength(1));

    act(() => {
      terminalSessionMock.instances[0].onState({ kind: "closed", reason: "stopped", code: 4405 });
    });
    fireEvent.click(screen.getByRole("button", { name: /^resume session$/i }));

    // The failing action still fires exactly once; zero would hide the
    // failure, while multiple calls would duplicate a bad resume request.
    await waitFor(() => expect(onResume).toHaveBeenCalledTimes(1));
    await screen.findByText("Couldn't resume session: host offline");
    // Failed resume must not remount xterm: the original closed bridge stays
    // visible so the user can retry after fixing the host.
    expect(terminalSessionMock.instances).toHaveLength(1);
  });

  it("names an offline sandbox in the resume action and its failures", async () => {
    const onResume = vi.fn().mockRejectedValue(new Error("launch failed"));
    render(
      <TerminalView
        sessionId="conv_abc"
        terminalId="terminal_bash_s1"
        onResume={onResume}
        resumeTarget="sandbox"
      />,
    );
    await waitFor(() => expect(terminalSessionMock.instances).toHaveLength(1));

    act(() => {
      terminalSessionMock.instances[0].onState({ kind: "closed", reason: "stopped", code: 4405 });
    });
    fireEvent.click(screen.getByRole("button", { name: /^resume sandbox$/i }));

    await screen.findByText("Couldn't resume sandbox: launch failed");
  });
});

describe("status overlay stacking", () => {
  it("keeps the connecting overlay inside the terminal's own stacking context", async () => {
    render(<TerminalView sessionId="conv_abc" terminalId="terminal_bash_s1" />);
    await waitFor(() => expect(terminalSessionMock.instances).toHaveLength(1));

    const view = screen.getByTestId("terminal-view");
    expect(view).toHaveAttribute("data-state", "connecting");
    expect(within(view).getByText("Connecting…")).toBeInTheDocument();
    // Without `isolate`, the overlay's z-index resolves at the page level and
    // paints over dropdowns portaled to <body> at z-50 (the session menu).
    expect(view).toHaveClass("isolate");
  });
});

describe("automatic reconnect", () => {
  beforeEach(() => {
    // Fake only what the backoff scheduling touches; promises and
    // queueMicrotask (which the mount path uses) stay real so React
    // act() flushes them naturally.
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "Date"] });
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  /** Mount the view and flush the deferred (microtask) session attach. */
  async function renderAndAttach(): Promise<void> {
    render(<TerminalView sessionId="conv_abc" terminalId="terminal_bash_s1" />);
    await act(async () => {});
    // Exactly one bridge per mount — see the closed-overlay tests.
    expect(terminalSessionMock.instances).toHaveLength(1);
  }

  /** Drive a close on the newest session instance. */
  function closeNewest(code: number): void {
    act(() => {
      terminalSessionMock.instances.at(-1)!.onState({
        kind: "closed",
        reason: `code ${code}`,
        code,
      });
    });
  }

  /** Advance past a backoff delay and flush the remount microtask. */
  async function elapse(ms: number): Promise<void> {
    act(() => {
      vi.advanceTimersByTime(ms);
    });
    await act(async () => {});
  }

  it("re-dials after a transport-level close (1006) once the backoff elapses", async () => {
    await renderAndAttach();

    closeNewest(1006);

    // Recovery is presented as recovery: the overlay must show the
    // reconnecting spinner, not the dead-end "Bridge closed" message.
    expect(screen.getByTestId("terminal-reconnecting")).toBeInTheDocument();
    expect(screen.queryByText(/Bridge closed/)).toBeNull();

    await elapse(RECONNECT_BACKOFF_MS[0]);
    // A second instance proves the keyed mount remounted and re-dialed;
    // still 1 would mean the close was treated as final.
    expect(terminalSessionMock.instances).toHaveLength(2);
    // The dead session was torn down explicitly. React 18 ignores the
    // callback-ref cleanup, so a missing dispose here means every
    // retry leaks an xterm instance and its listeners.
    expect(terminalSessionMock.instances[0].dispose).toHaveBeenCalled();
  });

  it("retries a rollout routing miss with the same OSS host key", async () => {
    vi.stubEnv("VITE_OMNIGENT_HOST_ROUTING", "true");
    setSessionHost("conv_abc", "host_oss");
    await renderAndAttach();
    const firstUrl = terminalSessionMock.instances[0].url;
    expect(new URL(firstUrl).searchParams.get("omnigent_slice_key")).toBe("host_oss");

    closeNewest(4400);
    await elapse(RECONNECT_BACKOFF_MS[0]);

    expect(terminalSessionMock.instances).toHaveLength(2);
    expect(terminalSessionMock.instances[1].url).toBe(firstUrl);
    expect(isHostKeyless("host_oss")).toBe(false);
  });

  it("re-dials after a code-less close (1005) — the redeploy-behind-ingress case", async () => {
    // A server redeploy behind a fronting proxy tears the attach socket
    // down without a clean app code reaching the browser, which reports
    // 1005 ("no status"). It must recover exactly like 1006, not dead-end
    // on "Bridge closed: code 1005".
    await renderAndAttach();

    closeNewest(1005);

    expect(screen.getByTestId("terminal-reconnecting")).toBeInTheDocument();
    expect(screen.queryByText(/Bridge closed/)).toBeNull();

    await elapse(RECONNECT_BACKOFF_MS[0]);
    expect(terminalSessionMock.instances).toHaveLength(2);
    expect(terminalSessionMock.instances[0].dispose).toHaveBeenCalled();
  });

  it("does not re-dial after a deliberate server close (4405 terminal-detached)", async () => {
    await renderAndAttach();

    closeNewest(4405);

    // Far beyond every backoff step: any scheduled re-dial would have
    // fired by now. A second instance would mean the policy resurrects
    // terminals the server intentionally ended.
    await elapse(60_000);
    expect(terminalSessionMock.instances).toHaveLength(1);
    expect(screen.getByText("Bridge closed: code 4405")).toBeInTheDocument();
    expect(screen.queryByTestId("terminal-reconnecting")).toBeNull();
  });

  it("stops re-dialing once the retry budget is exhausted", async () => {
    await renderAndAttach();

    // Each close→backoff cycle burns one budget entry. The re-dialed
    // connections never reach "connected", so the budget never resets.
    // Cycles are inherently serial: each backoff must elapse before the
    // next close can be driven.
    for (const [attempt, delay] of RECONNECT_BACKOFF_MS.entries()) {
      closeNewest(1006);
      // oxlint-disable-next-line no-await-in-loop
      await elapse(delay);
      // One new instance per attempt; a missing one means a backoff
      // step was skipped, an extra one means double-scheduling.
      expect(terminalSessionMock.instances).toHaveLength(attempt + 2);
    }

    closeNewest(1006);
    await elapse(60_000);
    // Budget exhausted: the final close sticks as the dead-end overlay
    // and no further sessions are constructed.
    expect(terminalSessionMock.instances).toHaveLength(RECONNECT_BACKOFF_MS.length + 1);
    expect(screen.getByText("Bridge closed: code 1006")).toBeInTheDocument();
    expect(screen.queryByTestId("terminal-reconnecting")).toBeNull();
  });

  it("restores the retry budget after a connection that stayed up past the stability window", async () => {
    await renderAndAttach();

    // Exhaust the budget with instant drops (serial by nature: each
    // backoff must elapse before the next close can be driven).
    for (const [, delay] of RECONNECT_BACKOFF_MS.entries()) {
      closeNewest(1006);
      // oxlint-disable-next-line no-await-in-loop
      await elapse(delay);
    }
    const exhausted = terminalSessionMock.instances.length;

    // The last re-dial succeeds and stays connected past the stability
    // window — this drop is a fresh outage, not the same flapping one.
    act(() => {
      terminalSessionMock.instances.at(-1)!.onState({ kind: "connected" });
    });
    await elapse(RECONNECT_STABLE_MS);
    closeNewest(1006);
    await elapse(RECONNECT_BACKOFF_MS[0]);

    // One more instance proves the budget reset; staying at `exhausted`
    // would mean a long-lived terminal gets only 5 reconnects per page
    // load instead of 5 per outage.
    expect(terminalSessionMock.instances).toHaveLength(exhausted + 1);
  });

  it("re-dials as soon as the tab becomes visible, without waiting out the backoff", async () => {
    // Simulate the report: the drop is discovered while the tab is
    // hidden, and the user returns before any timer fires.
    const visibility = { value: "hidden" as DocumentVisibilityState };
    Object.defineProperty(document, "visibilityState", {
      configurable: true,
      get: () => visibility.value,
    });
    try {
      await renderAndAttach();
      closeNewest(1006);

      // Still hidden: a visibilitychange that is not a reveal (e.g.
      // another hide event) must not trigger the re-dial.
      act(() => {
        document.dispatchEvent(new Event("visibilitychange"));
      });
      await act(async () => {});
      expect(terminalSessionMock.instances).toHaveLength(1);

      visibility.value = "visible";
      act(() => {
        document.dispatchEvent(new Event("visibilitychange"));
      });
      await act(async () => {});
      // The reveal re-dialed immediately — no timer was advanced, so a
      // missing second instance means the visibility path isn't wired.
      expect(terminalSessionMock.instances).toHaveLength(2);
    } finally {
      // Restore the default prototype getter for later tests.
      delete (document as { visibilityState?: unknown }).visibilityState;
    }
  });

  it.each([1006, 4400])(
    "re-dials a hidden terminal after exhausting retries for close %s",
    async (code) => {
      vi.stubEnv("VITE_OMNIGENT_HOST_ROUTING", "true");
      setSessionHost("conv_abc", "host_oss");
      // A warm surface parked behind another session's view: the transport
      // flaps with nobody watching and the background reconnect loop burns
      // its whole budget.
      const { rerender } = render(
        <TerminalView sessionId="conv_abc" terminalId="terminal_bash_s1" active={false} />,
      );
      await act(async () => {});
      expect(terminalSessionMock.instances).toHaveLength(1);

      for (const [, delay] of RECONNECT_BACKOFF_MS.entries()) {
        closeNewest(code);
        // oxlint-disable-next-line no-await-in-loop
        await elapse(delay);
      }
      closeNewest(code);
      await elapse(60_000);
      const exhausted = RECONNECT_BACKOFF_MS.length + 1;
      expect(terminalSessionMock.instances).toHaveLength(exhausted);

      // Reveal: a user is now looking at the dead pane — that is the retry
      // signal, same as a tab thaw. One fresh dial, budget restored.
      rerender(<TerminalView sessionId="conv_abc" terminalId="terminal_bash_s1" active />);
      await act(async () => {});
      expect(terminalSessionMock.instances).toHaveLength(exhausted + 1);
      expect(terminalSessionMock.instances.at(-1)!.url).toBe(terminalSessionMock.instances[0].url);
      expect(isHostKeyless("host_oss")).toBe(false);
    },
  );

  it("does not resurrect a deliberately closed terminal on reveal", async () => {
    const { rerender } = render(
      <TerminalView sessionId="conv_abc" terminalId="terminal_bash_s1" active={false} />,
    );
    await act(async () => {});
    closeNewest(4405);
    await elapse(60_000);
    expect(terminalSessionMock.instances).toHaveLength(1);

    // The server ended this terminal on purpose; revealing the surface
    // must keep the dead-end overlay, not loop on the same answer.
    rerender(<TerminalView sessionId="conv_abc" terminalId="terminal_bash_s1" active />);
    await act(async () => {});
    expect(terminalSessionMock.instances).toHaveLength(1);
    expect(screen.getByText("Bridge closed: code 4405")).toBeInTheDocument();
  });
});

describe("TerminalView phone key rows", () => {
  function stubPhoneViewport(width: number, height: number, coarse = true) {
    vi.stubGlobal("innerWidth", width);
    vi.stubGlobal("innerHeight", height);
    vi.stubGlobal("screen", { width, height });
    vi.stubGlobal(
      "matchMedia",
      vi.fn((query: string) => ({
        matches:
          query === "(max-width: 767.98px)"
            ? width < 768
            : query === "(pointer: coarse)"
              ? coarse
              : false,
        media: query,
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
      })),
    );
    vi.spyOn(window, "requestAnimationFrame").mockImplementation((cb: FrameRequestCallback) => {
      cb(0);
      return 1;
    });
  }

  function stubRootRect(width: number, height: number) {
    return vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(
      () =>
        ({
          top: 0,
          bottom: height,
          left: 0,
          right: width,
          width,
          height,
          x: 0,
          y: 0,
          toJSON: () => ({}),
        }) as DOMRect,
    );
  }

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("renders key rows on a writable phone terminal, suppresses auto-focus on connect and reveal, and disables keys while reconnecting", async () => {
    stubPhoneViewport(390, 844);
    stubRootRect(390, 788);

    const { rerender } = render(
      <TerminalView
        keySurface="shell"
        sessionId="conv_abc"
        terminalId="terminal_bash_s1"
        active={false}
      />,
    );
    await act(async () => {});
    expect(terminalSessionMock.instances).toHaveLength(1);
    expect(terminalSessionMock.instances[0].focusOnConnect).toBe(false);
    expect(screen.queryByTestId("terminal-key-row")).toBeNull();

    // Reveal on phone: key row mounts, session.focus() is not called automatically
    rerender(
      <TerminalView keySurface="shell" sessionId="conv_abc" terminalId="terminal_bash_s1" active />,
    );
    await act(async () => {});
    expect(terminalSessionMock.instances[0].focus).not.toHaveBeenCalled();
    const keyRow = screen.getByTestId("terminal-key-row");
    expect(keyRow).toHaveAttribute("aria-disabled", "true");

    act(() => {
      terminalSessionMock.instances[0].onState({ kind: "connected" });
    });
    expect(keyRow).toHaveAttribute("aria-disabled", "false");

    // Swipe pager to page 1 and verify transient error + transport close (1006)
    // keep the exact same DOM node disabled in place without resetting page.
    const pager = screen
      .getByTestId("terminal-key-row-pager")
      .querySelector("[data-pager]") as HTMLElement;
    fireEvent.pointerDown(pager, { button: 0, pointerId: 1, clientX: 200, clientY: 70 });
    fireEvent.pointerMove(pager, { pointerId: 1, clientX: 120, clientY: 70 });
    fireEvent.pointerUp(pager, { pointerId: 1, clientX: 120, clientY: 70 });
    expect(keyRow).toHaveAttribute("data-bottom-page", "1");

    act(() => {
      terminalSessionMock.instances[0].onState({ kind: "error" });
    });
    expect(screen.getByTestId("terminal-key-row")).toBe(keyRow);
    expect(keyRow).toHaveAttribute("aria-disabled", "true");
    expect(keyRow).toHaveAttribute("data-bottom-page", "1");

    act(() => {
      terminalSessionMock.instances[0].onState({ kind: "closed", code: 1006, reason: "transport" });
    });
    expect(screen.getByTestId("terminal-key-row")).toBe(keyRow);
    expect(keyRow).toHaveAttribute("aria-disabled", "true");
    expect(keyRow).toHaveAttribute("data-bottom-page", "1");

    // Deliberate close (1000) hides the key row
    act(() => {
      terminalSessionMock.instances[0].onState({ kind: "closed", code: 1000, reason: "" });
    });
    expect(screen.queryByTestId("terminal-key-row")).toBeNull();
  });

  it("hides key rows on readOnly, showKeyRows={false}, desktop viewports, or below the vertical room floor", async () => {
    stubPhoneViewport(390, 844);
    const rectSpy = stubRootRect(390, 788);

    const { rerender } = render(
      <TerminalView
        keySurface="shell"
        sessionId="conv_abc"
        terminalId="terminal_bash_s1"
        readOnly
      />,
    );
    await act(async () => {});
    expect(screen.queryByTestId("terminal-key-row")).toBeNull();

    rerender(
      <TerminalView
        keySurface="shell"
        sessionId="conv_abc"
        terminalId="terminal_bash_s1"
        showKeyRows={false}
      />,
    );
    await act(async () => {});
    expect(screen.queryByTestId("terminal-key-row")).toBeNull();

    // Portrait room floor: height 180 < 119 + 108 (227) -> hidden
    rectSpy.mockRestore();
    const tightSpy = stubRootRect(390, 180);
    rerender(
      <TerminalView keySurface="shell" sessionId="conv_abc" terminalId="terminal_bash_s1" />,
    );
    await act(async () => {});
    act(() => {
      window.dispatchEvent(new Event("resize"));
    });
    expect(screen.queryByTestId("terminal-key-row")).toBeNull();

    // Landscape 1-row bar with the page strip: width 667 >= 560, height 190 >= 73 + 108 (181) -> visible
    tightSpy.mockRestore();
    stubPhoneViewport(667, 375);
    stubRootRect(667, 190);
    act(() => {
      window.dispatchEvent(new Event("resize"));
    });
    expect(screen.getByTestId("terminal-key-row")).toBeInTheDocument();
  });

  it("uses width after side safe areas for the one-row room floor", async () => {
    stubPhoneViewport(844, 390);
    stubRootRect(590.8, 180);

    render(<TerminalView keySurface="shell" sessionId="conv_abc" terminalId="terminal_bash_s1" />);
    await act(async () => {});

    const view = screen.getByTestId("terminal-view");
    view.style.setProperty("--omnigent-safe-left", "47px");
    view.style.setProperty("--omnigent-safe-right", "47px");
    act(() => {
      window.dispatchEvent(new Event("resize"));
    });

    // 590.8 - 47 - 47 = 496.8px: two rows need 119 + 108px, not 73 + 108px.
    expect(screen.queryByTestId("terminal-key-row")).toBeNull();
  });

  it("uses the Native layout and toggle independently of Shell", async () => {
    stubPhoneViewport(390, 844);
    stubRootRect(390, 788);
    const { keys } = resolveTerminalKeys({ ...readMobileAssistantPreferences() });
    keys.shell.enabled = false;
    keys.native.top.keys[0].label = "Native key";
    expect(writeTerminalKeyPreferences({ terminalKeys: keys })).toBe("saved");
    const { rerender } = render(
      <TerminalView keySurface="shell" sessionId="conv_abc" terminalId="terminal_bash_s1" />,
    );
    await act(async () => {});
    expect(screen.queryByTestId("terminal-key-row")).toBeNull();
    rerender(
      <TerminalView keySurface="native" sessionId="conv_abc" terminalId="terminal_bash_s1" />,
    );
    expect(screen.getByRole("button", { name: "Native key" })).toBeInTheDocument();
    act(() =>
      writeTerminalKeyPreferences({
        terminalKeys: { ...keys, native: { ...keys.native, enabled: false } },
      }),
    );
    expect(screen.queryByTestId("terminal-key-row")).toBeNull();
  });

  it("uses custom order and reacts to live switches", async () => {
    stubPhoneViewport(390, 844);
    stubRootRect(390, 788);
    const initial = readMobileAssistantPreferences();
    const customKey = {
      id: "refresh",
      label: "Refresh",
      kind: "key" as const,
      chord: { code: "KeyR", modifiers: [] },
    };
    writeMobileAssistantPreferences({
      ...initial,
      terminalButtons: [customKey, ...initial.terminalButtons!],
    });
    render(<TerminalView keySurface="shell" sessionId="conv_abc" terminalId="terminal_bash_s1" />);
    await act(async () => {});
    act(() => terminalSessionMock.instances[0].onState({ kind: "connected" }));
    expect(
      within(screen.getByTestId("terminal-key-row-primary")).getAllByRole("button")[0],
    ).toHaveAccessibleName("Refresh");
    act(() =>
      writeMobileAssistantPreferences({
        ...readMobileAssistantPreferences(),
        terminalSecondRow: false,
      }),
    );
    expect(screen.queryByTestId("terminal-key-row-pager")).toBeNull();
    act(() =>
      writeMobileAssistantPreferences({
        ...readMobileAssistantPreferences(),
        terminalKeysEnabled: false,
      }),
    );
    expect(screen.queryByTestId("terminal-key-row")).toBeNull();
    act(() =>
      writeMobileAssistantPreferences({
        ...readMobileAssistantPreferences(),
        terminalKeysEnabled: true,
      }),
    );
    expect(screen.getByTestId("terminal-key-row")).toBeInTheDocument();
    expect(screen.queryByTestId("terminal-key-row-pager")).toBeNull();
  });
});

describe("terminal clipboard paste", () => {
  afterEach(() => vi.unstubAllGlobals());

  async function openTerminal(readOnly = false) {
    const view = render(
      <TerminalView sessionId="conv_paste" terminalId="terminal_a" readOnly={readOnly} />,
    );
    await waitFor(() => expect(terminalSessionMock.instances).toHaveLength(1));
    const terminal = terminalSessionMock.instances[0];
    act(() => terminal.onState({ kind: "connected" }));
    return { terminal, ...view };
  }

  it.each([true, false])("normalizes a single line with enter=%s", async (enter) => {
    vi.stubGlobal("navigator", { clipboard: { readText: async () => "mak\x1be\r\n\r\n" } });
    const { terminal } = await openTerminal();
    await act(async () =>
      terminal.onClipboardAction?.("paste", { enter, keyId: enter ? "paste" : undefined }),
    );
    expect(terminal.paste).toHaveBeenCalledExactlyOnceWith("make", enter);
  });

  it("normalizes native paste data without attempting an async clipboard read", async () => {
    const readText = vi.fn().mockRejectedValue(new Error("denied"));
    vi.stubGlobal("navigator", { clipboard: { readText } });
    const { terminal } = await openTerminal();
    await act(async () =>
      terminal.onClipboardAction?.("paste", { enter: false }, "a\x1bb\r\n\r\n"),
    );
    expect(readText).not.toHaveBeenCalled();
    expect(terminal.paste).toHaveBeenCalledExactlyOnceWith("ab", false);
  });

  it.each([true, false])("confirms multiline input with bracketed mode=%s", async (bracketed) => {
    const readText = vi.fn(async () => "a\r\nb\r\n");
    vi.stubGlobal("navigator", { clipboard: { readText } });
    const { terminal } = await openTerminal();
    terminal.pastesBracketed.mockReturnValue(bracketed);
    const tap = () => terminal.onClipboardAction?.("paste", { enter: true, keyId: "paste" });
    await act(async () => tap());
    expect(screen.getByRole("status")).toHaveTextContent("2 lines");
    expect(terminal.paste).not.toHaveBeenCalled();
    await act(async () => tap());
    expect(readText).toHaveBeenCalledTimes(1);
    if (!bracketed) {
      expect(terminal.paste).not.toHaveBeenCalled();
      fireEvent.click(screen.getByRole("button", { name: "Paste" }));
    }
    expect(terminal.paste).toHaveBeenCalledExactlyOnceWith("a\nb", false);
  });

  it("uses the paste box when clipboard access fails and reviews tabs", async () => {
    vi.stubGlobal("navigator", {
      clipboard: {
        readText: async () => {
          throw new Error("denied");
        },
      },
    });
    const { terminal } = await openTerminal();
    await act(async () => terminal.onClipboardAction?.("paste", { enter: false }));
    fireEvent.change(screen.getByLabelText("Text to paste"), { target: { value: "go\there" } });
    fireEvent.click(screen.getByRole("button", { name: "Send to terminal" }));
    expect(screen.getByText("go⇥here")).toBeInTheDocument();
    expect(terminal.paste).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Paste" }));
    expect(terminal.paste).toHaveBeenCalledExactlyOnceWith("go\there", false);
  });

  it("drops both a queued review and a delayed host reply when the terminal changes", async () => {
    vi.stubGlobal("navigator", { clipboard: { readText: async () => "a\nb" } });
    const { terminal, rerender } = await openTerminal();
    await act(async () => terminal.onClipboardAction?.("paste", { enter: false }));
    expect(screen.getByRole("status")).toHaveTextContent("2 lines");
    let reply: (result: { text: string }) => void = () => {};
    terminal.readHostClipboard.mockReturnValue(
      new Promise((resolve) => {
        reply = resolve;
      }),
    );
    act(() => terminal.onClipboardAction?.("pasteHost", { enter: true, keyId: "host" }));
    expect(screen.queryByText("2 lines", { exact: false })).not.toBeInTheDocument();
    rerender(<TerminalView sessionId="conv_paste" terminalId="terminal_b" />);
    await waitFor(() => expect(terminalSessionMock.instances).toHaveLength(2));
    const other = terminalSessionMock.instances[1];
    act(() => other.onState({ kind: "connected" }));
    await act(async () => reply({ text: "pwd" }));
    expect(terminal.paste).not.toHaveBeenCalled();
    expect(other.paste).not.toHaveBeenCalled();
  });

  it("does not ask a read-only connection to read its host clipboard", async () => {
    const { terminal } = await openTerminal(true);
    await act(async () => terminal.onClipboardAction?.("pasteHost", { enter: true }));
    expect(terminal.readHostClipboard).not.toHaveBeenCalled();
    expect(terminal.paste).not.toHaveBeenCalled();
  });
});
