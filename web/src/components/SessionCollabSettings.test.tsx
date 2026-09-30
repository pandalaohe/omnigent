import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";

import { readHostColorPreferences } from "@/lib/hostColorPreferences";
import { readSessionCollabPreferences } from "@/lib/sessionCollabPreferences";
import { SessionCollabSettings } from "./SessionCollabSettings";

vi.mock("@/hooks/useHosts", () => ({
  useHosts: () => ({
    data: [
      { host_id: "host-1", name: "TMB", owner: "u", status: "online" },
      { host_id: "host-2", name: "fn", owner: "u", status: "online" },
    ],
  }),
}));

const LABELS = [
  "Enable session collaboration",
  "Session open rate limit",
  "Relay depth limit",
  "Rate per session pair",
  "Rate per sending session",
  "Duplicate message window",
  "Undelivered message lifetime",
  "Timed flows",
  "Keep idle children warm",
  "Claude keep-warm interval",
  "Codex keep-warm interval",
  "Longest keep-warm run",
];

function renderSettings() {
  return render(
    <MemoryRouter>
      <SessionCollabSettings />
    </MemoryRouter>,
  );
}

beforeEach(() => localStorage.clear());
afterEach(cleanup);

describe("SessionCollabSettings", () => {
  it("renders the setting labels with keep-warm off by default", () => {
    renderSettings();

    for (const label of LABELS) {
      expect(screen.getAllByText(label).length).toBeGreaterThan(0);
    }
    expect(screen.getByRole("switch", { name: "Keep idle children warm" })).not.toBeChecked();
  });

  it("switching row 0 off disables the rows below", () => {
    renderSettings();

    fireEvent.click(screen.getByRole("switch", { name: "Enable session collaboration" }));

    expect(readSessionCollabPreferences().enabled).toBe(false);
    expect(screen.getByLabelText("Relay depth limit")).toBeDisabled();
    expect(screen.getByLabelText("Rate per session pair")).toBeDisabled();
    expect(screen.getByLabelText("Undelivered message lifetime in minutes")).toBeDisabled();
    expect(screen.getByRole("switch", { name: "Timed flows" })).toBeDisabled();
    expect(screen.getByRole("switch", { name: "Keep idle children warm" })).toBeDisabled();
    expect(screen.getByLabelText("Claude keep-warm interval in minutes")).toBeDisabled();
    expect(screen.getByLabelText("Codex keep-warm interval in minutes")).toBeDisabled();
    expect(screen.getByLabelText("Longest keep-warm run in hours")).toBeDisabled();
  });

  it("writes keep-warm intervals entered in minutes and hours", () => {
    renderSettings();
    fireEvent.click(screen.getByRole("switch", { name: "Keep idle children warm" }));
    expect(readSessionCollabPreferences().childKeepWarmEnabled).toBe(true);
    const claude = screen.getByLabelText("Claude keep-warm interval in minutes");
    const codex = screen.getByLabelText("Codex keep-warm interval in minutes");
    const longest = screen.getByLabelText("Longest keep-warm run in hours");

    expect(claude).toHaveValue(55);
    expect(codex).toHaveValue(25);
    expect(longest).toHaveValue(8);

    fireEvent.change(claude, { target: { value: "30" } });
    fireEvent.change(codex, { target: { value: "5" } });
    fireEvent.change(longest, { target: { value: "4" } });

    expect(readSessionCollabPreferences().childKeepWarmClaudeIntervalSeconds).toBe(1800);
    expect(readSessionCollabPreferences().childKeepWarmCodexIntervalSeconds).toBe(300);
    expect(readSessionCollabPreferences().childKeepWarmMaxSeconds).toBe(14400);

    fireEvent.change(claude, { target: { value: "55" } });
    fireEvent.change(longest, { target: { value: "8" } });

    expect(readSessionCollabPreferences().childKeepWarmClaudeIntervalSeconds).toBe(3300);
    expect(readSessionCollabPreferences().childKeepWarmMaxSeconds).toBe(28800);
  });

  it("writes a valid relay depth while typing", () => {
    renderSettings();
    const input = screen.getByLabelText("Relay depth limit");

    fireEvent.change(input, { target: { value: "3" } });

    expect(readSessionCollabPreferences().relayDepthMax).toBe(3);
    expect(input).toHaveValue(3);
  });

  it("writes an undelivered message lifetime entered in minutes", () => {
    renderSettings();
    const input = screen.getByLabelText("Undelivered message lifetime in minutes");

    expect(input).toHaveValue(1440);

    fireEvent.change(input, { target: { value: "5" } });

    expect(readSessionCollabPreferences().undeliveredTtlSeconds).toBe(300);
    expect(input).toHaveValue(5);
  });

  it("writes nothing for invalid input and restores the previous value on blur", () => {
    renderSettings();
    const input = screen.getByLabelText("Relay depth limit");

    fireEvent.change(input, { target: { value: "abc" } });
    expect(readSessionCollabPreferences().relayDepthMax).toBe(30);
    fireEvent.blur(input);
    expect(input).toHaveValue(30);

    fireEvent.change(input, { target: { value: "0" } });
    expect(readSessionCollabPreferences().relayDepthMax).toBe(30);
    fireEvent.blur(input);
    expect(input).toHaveValue(30);
  });

  it("renders a colour row per host, automatic until a swatch is picked", () => {
    renderSettings();

    const rows = screen.getAllByTestId("host-color-row");
    expect(rows).toHaveLength(2);
    expect(rows[0]).toHaveTextContent("TMB");
    expect(rows[0]).toHaveTextContent("Automatic");
    // Eight palette swatches per host.
    expect(within(rows[0]).getAllByRole("button")).toHaveLength(8);
    expect(screen.queryByRole("button", { name: "Reset to automatic" })).toBeNull();
  });

  it("picks one host colour and resets it to automatic", () => {
    renderSettings();

    fireEvent.click(screen.getByRole("button", { name: "TMB colour: purple" }));

    expect(readHostColorPreferences()).toEqual({ "host-1": "purple" });
    expect(screen.getByRole("button", { name: "Reset to automatic" })).toBeInTheDocument();
    // The other host stays automatic.
    expect(screen.getAllByTestId("host-color-row")[1]).toHaveTextContent("Automatic");

    fireEvent.click(screen.getByRole("button", { name: "Reset to automatic" }));

    expect(readHostColorPreferences()).toEqual({ "host-1": "auto" });
    expect(screen.queryByRole("button", { name: "Reset to automatic" })).toBeNull();
    expect(screen.getAllByTestId("host-color-row")[0]).toHaveTextContent("Automatic");
  });
});
