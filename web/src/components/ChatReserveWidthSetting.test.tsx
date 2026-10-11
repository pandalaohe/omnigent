import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";
import { readPanelSizePreference } from "@/lib/panelSizePreferences";
import { ChatReserveWidthSetting } from "./ChatReserveWidthSetting";

afterEach(() => {
  cleanup();
  localStorage.clear();
});

it("saves a valid reserve, rejects out-of-range drafts, and resets to 600", () => {
  render(<ChatReserveWidthSetting />);
  const input = screen.getByRole("spinbutton", { name: "Chat reserve width" });
  expect(input).toHaveValue(600);
  fireEvent.change(input, { target: { value: "800" } });
  expect(readPanelSizePreference("inlinePanelChatReserveWidthPx")).toBe(800);
  fireEvent.change(input, { target: { value: "1400" } });
  fireEvent.blur(input);
  expect(input).toHaveValue(800);
  expect(readPanelSizePreference("inlinePanelChatReserveWidthPx")).toBe(800);
  fireEvent.click(screen.getByRole("button", { name: "Reset" }));
  expect(input).toHaveValue(600);
  expect(readPanelSizePreference("inlinePanelChatReserveWidthPx")).toBeNull();
});
