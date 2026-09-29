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
  "Default inbound policy for new sessions",
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
  it("renders the nine setting labels", () => {
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
    expect(screen.getByLabelText("Default inbound policy for new sessions")).toBeDisabled();
    expect(screen.getByRole("switch", { name: "Timed flows" })).toBeDisabled();
  });

  it("writes a valid relay depth while typing", () => {
    renderSettings();
    const input = screen.getByLabelText("Relay depth limit");

    fireEvent.change(input, { target: { value: "3" } });

    expect(readSessionCollabPreferences().relayDepthMax).toBe(3);
    expect(input).toHaveValue(3);
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
