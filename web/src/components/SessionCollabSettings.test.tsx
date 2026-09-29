import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { MemoryRouter } from "react-router-dom";

import { readSessionCollabPreferences } from "@/lib/sessionCollabPreferences";
import { SessionCollabSettings } from "./SessionCollabSettings";

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
});
