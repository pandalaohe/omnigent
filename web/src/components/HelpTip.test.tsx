import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { HelpTip } from "./HelpTip";

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe("HelpTip", () => {
  it("shows the explanation on keyboard focus without moving focus", () => {
    render(<HelpTip label="About the folder">Sessions open here.</HelpTip>);
    const trigger = screen.getByRole("button", { name: "About the folder" });
    act(() => trigger.focus());
    expect(screen.getByText("Sessions open here.")).toBeInTheDocument();
    expect(trigger).toHaveFocus();
    fireEvent.keyDown(trigger, { key: "Escape", code: "Escape" });
    expect(screen.queryByText("Sessions open here.")).not.toBeInTheDocument();
  });
  it("shows the hint on click and hides it on Escape", async () => {
    render(<HelpTip label="About relay depth">Held until you release it.</HelpTip>);

    expect(screen.queryByText("Held until you release it.")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "About relay depth" }));
    const hint = await screen.findByText("Held until you release it.");
    expect(hint).toBeInTheDocument();

    fireEvent.keyDown(hint, { key: "Escape", code: "Escape" });
    await waitFor(() =>
      expect(screen.queryByText("Held until you release it.")).not.toBeInTheDocument(),
    );
  });

  it("stays open while the mouse moves from the trigger into the hint", () => {
    vi.useFakeTimers();
    render(<HelpTip label="About relay depth">Held until you release it.</HelpTip>);

    const trigger = screen.getByRole("button", { name: "About relay depth" });
    fireEvent.pointerEnter(trigger, { pointerType: "mouse" });
    expect(screen.getByText("Held until you release it.")).toBeInTheDocument();

    // Leaving the trigger schedules a close; entering the content in time
    // cancels it, so the hint stays open.
    fireEvent.pointerLeave(trigger, { pointerType: "mouse" });
    act(() => {
      vi.advanceTimersByTime(100);
    });
    expect(screen.getByText("Held until you release it.")).toBeInTheDocument();

    const hint = screen.getByText("Held until you release it.");
    fireEvent.pointerEnter(hint, { pointerType: "mouse" });
    act(() => {
      vi.advanceTimersByTime(1000);
    });
    expect(screen.getByText("Held until you release it.")).toBeInTheDocument();

    // Leaving the content closes it once the delay elapses.
    fireEvent.pointerLeave(hint, { pointerType: "mouse" });
    act(() => {
      vi.advanceTimersByTime(1000);
    });
    expect(screen.queryByText("Held until you release it.")).not.toBeInTheDocument();
  });
});
