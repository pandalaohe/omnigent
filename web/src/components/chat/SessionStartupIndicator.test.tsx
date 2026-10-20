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
    expect(screen.getByRole("status")).toHaveTextContent("Your message is saved");
    expect(screen.queryByText("Working")).toBeNull();
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
