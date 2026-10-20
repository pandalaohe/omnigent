import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { SessionStartupIndicator } from "./SessionStartupIndicator";

afterEach(cleanup);

const queued = {
  id: "startup",
  attemptId: null,
  inputId: "input",
  state: "queued" as const,
  reason: null,
  elapsedSeconds: 45,
  queuePositionEstimate: 3,
  canCancel: true,
  evidence: { runtimeReady: false, toolsReady: false, inputAccepted: false, workStarted: false },
};

describe("SessionStartupIndicator", () => {
  it("shows a durable queued message and an estimated position without claiming work", () => {
    render(<SessionStartupIndicator startup={queued} onCancel={vi.fn()} />);
    expect(screen.getByRole("status")).toHaveTextContent("Waiting to start on this host");
    expect(screen.getByRole("status")).toHaveTextContent("About 3 in queue");
    expect(screen.getByRole("status")).toHaveTextContent("Waiting 45s since request");
    expect(screen.getByRole("status")).toHaveTextContent("Your message is saved");
    expect(screen.queryByText("Working")).toBeNull();
  });

  it("shows the first-message wait without claiming readiness", () => {
    render(
      <SessionStartupIndicator
        startup={{ ...queued, inputId: null, state: "waiting_input", admittedElapsedSeconds: 8 }}
        onCancel={vi.fn()}
      />,
    );
    expect(screen.getByRole("status")).toHaveTextContent("Waiting for your first message");
    expect(screen.getByRole("status")).toHaveTextContent("8s since host admission");
    expect(screen.getByRole("status")).not.toHaveTextContent("since first message");
    expect(screen.getByText("Waiting for your first message")).not.toHaveTextContent("ready");
  });

  it("separates request, admission, and first-message clocks", () => {
    render(
      <SessionStartupIndicator
        startup={{
          ...queued,
          state: "verifying",
          elapsedSeconds: 400,
          admittedElapsedSeconds: 185,
          inputElapsedSeconds: 121,
        }}
        onCancel={vi.fn()}
      />,
    );
    const status = screen.getByRole("status");
    expect(status).toHaveTextContent("400s since request");
    expect(status).toHaveTextContent("185s since host admission");
    expect(status).toHaveTextContent("121s since first message");
  });

  it("warns after 30 admitted seconds, not after 30 seconds in the queue", () => {
    const onCancel = vi.fn();
    const { rerender } = render(
      <SessionStartupIndicator startup={{ ...queued, elapsedSeconds: 45 }} onCancel={onCancel} />,
    );
    expect(screen.getByRole("status")).not.toHaveTextContent("Startup is taking longer");

    rerender(
      <SessionStartupIndicator
        startup={{ ...queued, state: "starting", admittedElapsedSeconds: 29 }}
        onCancel={onCancel}
      />,
    );
    expect(screen.getByRole("status")).not.toHaveTextContent("Startup is taking longer");

    rerender(
      <SessionStartupIndicator
        startup={{ ...queued, state: "verifying", admittedElapsedSeconds: 30 }}
        onCancel={onCancel}
      />,
    );
    expect(screen.getByRole("status")).toHaveTextContent(
      "Startup is taking longer than expected. Your message remains saved.",
    );
    expect(screen.getByRole("status")).not.toHaveTextContent("failed");
    expect(screen.getByRole("status")).not.toHaveTextContent("retry");
  });

  it("explains paused admissions and observation deadlines without implying retry", () => {
    const { rerender } = render(
      <SessionStartupIndicator
        startup={{ ...queued, unconfirmedStartups: 2 }}
        onCancel={vi.fn()}
      />,
    );
    expect(screen.getByRole("status")).toHaveTextContent(
      "Host startups are paused until earlier unconfirmed startups are reconciled.",
    );
    rerender(
      <SessionStartupIndicator
        startup={{ ...queued, state: "blocked", reason: "needs_attention", elapsedSeconds: 86400 }}
        onCancel={vi.fn()}
      />,
    );
    expect(screen.getByRole("status")).toHaveTextContent(
      "This request has waited 24 hours and needs attention.",
    );
    rerender(
      <SessionStartupIndicator
        startup={{ ...queued, state: "uncertain", reason: "runtime_readiness_unconfirmed" }}
        onCancel={vi.fn()}
      />,
    );
    expect(screen.getByRole("status")).toHaveTextContent("Host readiness could not be confirmed.");
    rerender(
      <SessionStartupIndicator
        startup={{ ...queued, state: "uncertain", reason: "input_start_unconfirmed" }}
        onCancel={vi.fn()}
      />,
    );
    expect(screen.getByRole("status")).toHaveTextContent(
      "Work after your first message could not be confirmed.",
    );
    rerender(
      <SessionStartupIndicator
        startup={{ ...queued, state: "uncertain", reason: "runtime_observation_pending" }}
        onCancel={vi.fn()}
      />,
    );
    expect(screen.getByRole("status")).toHaveTextContent(
      "Waiting to confirm that the host is ready.",
    );
    rerender(
      <SessionStartupIndicator
        startup={{ ...queued, state: "uncertain", reason: "input_observation_pending" }}
        onCancel={vi.fn()}
      />,
    );
    expect(screen.getByRole("status")).toHaveTextContent(
      "Waiting to confirm work after your first message.",
    );
  });

  it("keeps uncertain work visible and prevents duplicate stop requests", async () => {
    let finish!: () => void;
    const cancel = vi.fn(
      () =>
        new Promise<void>((resolve) => {
          finish = resolve;
        }),
    );
    render(
      <SessionStartupIndicator startup={{ ...queued, state: "uncertain" }} onCancel={cancel} />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Stop startup" }));
    expect(screen.getByRole("button", { name: "Stopping…" })).toBeDisabled();
    expect(cancel).toHaveBeenCalledOnce();
    expect(cancel).toHaveBeenCalledWith("startup", null);
    finish();
    await waitFor(() => expect(screen.getByRole("button", { name: "Stop startup" })).toBeEnabled());
  });

  it("reveals real receipt flags and hides controls for readers", () => {
    render(<SessionStartupIndicator startup={queued} onCancel={vi.fn()} readOnly />);
    fireEvent.click(screen.getByText("Inspect startup"));
    expect(screen.getByText("First message recorded: no")).toBeVisible();
    expect(screen.queryByRole("button", { name: "Stop startup" })).toBeNull();
  });

  it("removes the startup band once work is positively confirmed", () => {
    render(
      <SessionStartupIndicator startup={{ ...queued, state: "working" }} onCancel={vi.fn()} />,
    );
    expect(screen.queryByRole("status")).toBeNull();
  });
});
