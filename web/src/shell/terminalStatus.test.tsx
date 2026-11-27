import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import type { ConnectionState } from "@/lib/terminalCloseCodes";
import { setTerminalLaunching, type TerminalInfo } from "@/lib/terminals";
import { deriveTerminalStatus, TerminalStatusBadge } from "./terminalStatus";

afterEach(() => {
  cleanup();
});

function terminal(overrides: Partial<TerminalInfo> = {}): TerminalInfo {
  return {
    id: "terminal_bash_s1",
    name: "bash",
    session: "s1",
    running: true,
    ...overrides,
  };
}

describe("deriveTerminalStatus", () => {
  it("shows an attached running terminal as idle without using global conversation state", () => {
    expect(deriveTerminalStatus(terminal({ running: true }), null)).toBe("idle");
  });

  it("shows recent terminal output as active", () => {
    expect(deriveTerminalStatus(terminal({ running: true }), { kind: "connected" }, true)).toBe(
      "active",
    );
  });

  it("falls back to resource state when connection state is null (unmounted terminal)", () => {
    // null connection state means the TerminalView is not mounted — status
    // must be derived from the resource alone, not a stale bridge state.
    expect(deriveTerminalStatus(terminal({ running: true }), null)).toBe("idle");
    expect(deriveTerminalStatus(terminal({ running: false }), null)).toBe("exited");
  });

  it("shows a stopped terminal resource as exited", () => {
    expect(deriveTerminalStatus(terminal({ running: false }), null)).toBe("exited");
  });

  it("gives confirmed exited resource precedence over stale transport state, except while launching", () => {
    expect(
      deriveTerminalStatus(
        terminal({ running: false }),
        { kind: "closed", reason: "code 1006", code: 1006, reconnecting: true },
        false,
      ),
    ).toBe("exited");
    expect(deriveTerminalStatus(terminal({ running: false }), { kind: "connecting" }, false)).toBe(
      "exited",
    );

    const checkLaunching = (mode: "wait" | "attach") => {
      setTerminalLaunching("terminal_bash_launch", mode);
      try {
        expect(
          deriveTerminalStatus(
            terminal({ id: "terminal_bash_launch", running: false }),
            { kind: "connecting" },
            false,
          ),
        ).toBe("connecting");
        expect(
          deriveTerminalStatus(
            terminal({ id: "terminal_bash_launch", running: false }),
            { kind: "closed", reason: "not found", code: 4404 },
            false,
          ),
        ).toBe("connecting");
      } finally {
        setTerminalLaunching("terminal_bash_launch", null);
      }
    };
    checkLaunching("wait");
    checkLaunching("attach");
  });

  it.each([
    [{ kind: "connecting" }, "connecting"],
    [{ kind: "connecting", reconnecting: true }, "reconnecting"],
    [{ kind: "error" }, "error"],
    [{ kind: "closed", reason: "code 1006", code: 1006, reconnecting: true }, "reconnecting"],
    [{ kind: "closed", reason: "code 1006", code: 1006 }, "disconnected"],
    [{ kind: "closed", reason: "code 1005", code: 1005 }, "disconnected"],
    [{ kind: "closed", reason: "wrong replica", code: 4400 }, "disconnected"],
    [{ kind: "closed", reason: "terminal session ended", code: 4404 }, "exited"],
    [{ kind: "closed", reason: "terminal detached", code: 4405 }, "closed"],
  ] satisfies [ConnectionState, string][])(
    "lets the active bridge state distinguish reconnecting, disconnected, exited, and closed",
    (connectionState, expected) => {
      expect(deriveTerminalStatus(terminal({ running: true }), connectionState, true)).toBe(
        expected,
      );
    },
  );
});

describe("TerminalStatusBadge", () => {
  it("renders a visible idle label for terminal selectors", () => {
    render(<TerminalStatusBadge status="idle" />);

    expect(screen.getByText("Idle")).toBeInTheDocument();
    expect(screen.getByLabelText("Idle")).toBeInTheDocument();
  });

  it("keeps dot size consistent while distinguishing reconnecting, disconnected, and exited", () => {
    const { rerender } = render(<TerminalStatusBadge status="active" />);

    let dot = screen.getByLabelText("Active").querySelector("span");
    expect(dot).toHaveClass("size-1.5", "bg-emerald-500");
    expect(dot).not.toHaveClass("animate-pulse");

    rerender(<TerminalStatusBadge status="reconnecting" />);
    dot = screen.getByLabelText("Reconnecting").querySelector("span");
    expect(dot).toHaveClass("size-1.5", "bg-amber-500", "animate-pulse");

    rerender(<TerminalStatusBadge status="disconnected" />);
    dot = screen.getByLabelText("Disconnected").querySelector("span");
    expect(dot).toHaveClass("size-1.5", "bg-amber-500");
    expect(dot).not.toHaveClass("animate-pulse");

    rerender(<TerminalStatusBadge status="exited" />);
    dot = screen.getByLabelText("Exited").querySelector("span");
    expect(dot).toHaveClass("size-1.5", "bg-black", "dark:bg-white");

    rerender(<TerminalStatusBadge status="closed" />);
    dot = screen.getByLabelText("Closed").querySelector("span");
    expect(dot).toHaveClass("size-1.5", "bg-black", "dark:bg-white");
  });
});
