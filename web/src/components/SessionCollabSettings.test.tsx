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
  it("renders the eight setting labels", () => {
    renderSettings();

    for (const label of LABELS) {
      expect(screen.getAllByText(label).length).toBeGreaterThan(0);
    }
  });

  it("switching row 0 off disables the rows below", () => {
    renderSettings();

    fireEvent.click(screen.getByRole("switch", { name: "Enable session collaboration" }));

    expect(readSessionCollabPreferences().enabled).toBe(false);
    expect(screen.getByLabelText("Relay depth limit")).toBeDisabled();
    expect(screen.getByLabelText("Rate per session pair")).toBeDisabled();
    expect(screen.getByLabelText("Undelivered message lifetime in minutes")).toBeDisabled();
    expect(screen.getByRole("switch", { name: "Timed flows" })).toBeDisabled();
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

    expect(readHostColorPreferences()).toEqual({});
    expect(screen.queryByRole("button", { name: "Reset to automatic" })).toBeNull();
  });
});
