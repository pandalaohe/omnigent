import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";

import {
  APPROVAL_TIMEOUT_STORAGE_KEY,
  readApprovalTimeoutPreferences,
} from "@/lib/approvalTimeoutPreferences";
import { ApprovalTimeoutSettings } from "./ApprovalTimeoutSettings";

describe("ApprovalTimeoutSettings", () => {
  beforeEach(() => localStorage.clear());

  it("defaults to 50 min with the turn stop on", () => {
    render(<ApprovalTimeoutSettings />);

    expect(screen.getByLabelText("Timeout")).toHaveValue(50);
    expect(
      screen.getByRole("switch", { name: "Stop the turn when the timeout expires" }),
    ).toBeChecked();
    expect(localStorage.getItem(APPROVAL_TIMEOUT_STORAGE_KEY)).toBeNull();
  });

  it("persists a changed timeout in minutes", async () => {
    render(<ApprovalTimeoutSettings />);
    const input = screen.getByLabelText("Timeout");

    fireEvent.change(input, { target: { value: "10" } });

    await waitFor(() => expect(readApprovalTimeoutPreferences().timeoutMinutes).toBe(10));
  });

  it("lets the user disable the turn stop", async () => {
    render(<ApprovalTimeoutSettings />);
    const toggle = screen.getByRole("switch", {
      name: "Stop the turn when the timeout expires",
    });

    fireEvent.click(toggle);

    await waitFor(() => expect(readApprovalTimeoutPreferences().stopTurn).toBe(false));
  });

  it("removes the stored key when every value is back at its default", async () => {
    localStorage.setItem(
      APPROVAL_TIMEOUT_STORAGE_KEY,
      JSON.stringify({ timeoutMinutes: 10, stopTurn: false }),
    );
    render(<ApprovalTimeoutSettings />);

    fireEvent.change(screen.getByLabelText("Timeout"), { target: { value: "50" } });
    await waitFor(() => expect(readApprovalTimeoutPreferences().timeoutMinutes).toBe(50));
    fireEvent.click(screen.getByRole("switch", { name: "Stop the turn when the timeout expires" }));

    await waitFor(() => expect(localStorage.getItem(APPROVAL_TIMEOUT_STORAGE_KEY)).toBeNull());
    expect(readApprovalTimeoutPreferences()).toEqual({ timeoutMinutes: 50, stopTurn: true });
  });
});
