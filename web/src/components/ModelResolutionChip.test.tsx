import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ModelResolutionChip } from "./ModelResolutionChip";

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe("ModelResolutionChip", () => {
  it("renders nothing when requested, launched, and running match (including Claude aliases)", () => {
    const { container } = render(
      <ModelResolutionChip
        session={{
          harness: "claude-native",
          requested: "opus",
          launched: "opus",
          running: "claude-opus-5-5",
          codexModelOptions: [{ id: "opus", model: "claude-opus-5-5", displayName: "Opus 5.5" }],
          labels: {},
        }}
      />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("renders an icon-only amber warning trigger and opens the popover on hover, click-pin, and keyboard focus", () => {
    vi.useFakeTimers();
    render(
      <ModelResolutionChip
        session={{
          harness: "claude-native",
          requested: "opus",
          launched: "opus",
          running: "claude-sonnet-5-5",
          codexModelOptions: [{ id: "opus", model: "claude-opus-5-5", displayName: "Opus 5.5" }],
          labels: {},
        }}
      />,
    );

    const trigger = screen.getByRole("button", { name: "Model resolution warning" });
    expect(trigger).toHaveAttribute("data-testid", "model-resolution-warning");
    expect(trigger.textContent?.trim()).toBe("");
    expect(trigger.querySelector("svg")).not.toBeNull();
    expect(screen.queryByTestId("model-resolution-popover")).toBeNull();

    // Hover opens the popover and pointer leave closes it after the grace delay.
    fireEvent.pointerEnter(trigger, { pointerType: "mouse" });
    expect(screen.getByTestId("model-resolution-popover")).toBeInTheDocument();
    expect(screen.getByText("Model resolution")).toBeInTheDocument();
    expect(screen.getByText("Requested")).toBeInTheDocument();
    expect(screen.getByText("Launched")).toBeInTheDocument();
    expect(screen.getByText("Running")).toBeInTheDocument();
    expect(screen.getByText("claude-sonnet-5-5")).toBeInTheDocument();

    fireEvent.pointerLeave(trigger, { pointerType: "mouse" });
    act(() => {
      vi.advanceTimersByTime(200);
    });
    expect(screen.queryByTestId("model-resolution-popover")).toBeNull();

    // Click pins the popover open even after pointer leave; Escape dismisses it.
    fireEvent.pointerEnter(trigger, { pointerType: "mouse" });
    fireEvent.click(trigger);
    fireEvent.pointerLeave(trigger, { pointerType: "mouse" });
    act(() => {
      vi.advanceTimersByTime(200);
    });
    expect(screen.getByTestId("model-resolution-popover")).toBeInTheDocument();

    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByTestId("model-resolution-popover")).toBeNull();

    // Keyboard focus opens the popover and blur closes it.
    fireEvent.focus(trigger);
    expect(screen.getByTestId("model-resolution-popover")).toBeInTheDocument();
    fireEvent.blur(trigger);
    act(() => {
      vi.advanceTimersByTime(200);
    });
    expect(screen.queryByTestId("model-resolution-popover")).toBeNull();
  });

  it("shows resolution notes when models match but resolution notes exist", () => {
    render(
      <ModelResolutionChip
        session={{
          harness: "codex-native",
          requested: "gpt-6.2-sol",
          launched: "gpt-6.2-sol",
          running: "gpt-6.2-sol",
          labels: {
            "omnigent.model_resolution": JSON.stringify([
              {
                setting: "explicit",
                saved: "old/model-a",
                model: "new/model-a",
                outcome: "renamed",
                host: "host-a",
              },
            ]),
          },
        }}
      />,
    );

    const trigger = screen.getByRole("button", { name: "Model resolution warning" });
    fireEvent.click(trigger);
    expect(screen.getByText("Model resolution")).toBeInTheDocument();
    expect(screen.getByText(/old\/model-a/)).toBeInTheDocument();
    expect(screen.getByText("Resolved automatically for this host.")).toBeInTheDocument();
  });

  it("resets pinned popover state when a warning clears and later reappears", () => {
    const base = {
      harness: "claude-native",
      requested: "opus",
      launched: "opus",
      labels: {},
    };
    const { rerender } = render(
      <ModelResolutionChip session={{ ...base, running: "claude-sonnet-5-5" }} />,
    );

    fireEvent.click(screen.getByTestId("model-resolution-warning"));
    expect(screen.getByTestId("model-resolution-popover")).toBeInTheDocument();

    rerender(<ModelResolutionChip session={{ ...base, running: "claude-opus-5-5" }} />);
    expect(screen.queryByTestId("model-resolution-warning")).toBeNull();

    rerender(<ModelResolutionChip session={{ ...base, running: "claude-haiku-4-5" }} />);
    expect(screen.getByTestId("model-resolution-warning")).toBeInTheDocument();
    expect(screen.queryByTestId("model-resolution-popover")).toBeNull();
  });
});
