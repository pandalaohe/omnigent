import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ProjectOrchestrationDraft } from "./ProjectOrchestrationView";
import { ProjectOrchestrationView } from "./ProjectOrchestrationView";

vi.mock("@/lib/standingApprovalApi", () => ({
  getStandingApprovalDefaults: vi.fn().mockResolvedValue({
    kinds: ["install", "merge", "release", "answer-child-approvals"],
    exceptions: [],
  }),
}));

vi.mock("@/lib/sessionChoiceApi", () => ({
  getSessionChoiceSettings: vi.fn().mockResolvedValue({
    enabled: true,
    active_preset: "balanced",
    presets: { balanced: { label: "Balanced" } },
  }),
}));

afterEach(cleanup);

function renderWithQuery(
  initial?: ProjectOrchestrationDraft,
  onChangeSpy?: (v: ProjectOrchestrationDraft | undefined) => void,
) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  function Harness() {
    const [val, setVal] = useState<ProjectOrchestrationDraft | undefined>(initial);
    return (
      <ProjectOrchestrationView
        value={val}
        onChange={(next) => {
          setVal(next);
          onChangeSpy?.(next);
        }}
      />
    );
  }
  return render(
    <QueryClientProvider client={client}>
      <Harness />
    </QueryClientProvider>,
  );
}

describe("ProjectOrchestrationView", () => {
  it("shows one line when nothing is overridden and expands inline on Customize", () => {
    const spy = vi.fn();
    renderWithQuery(undefined, spy);
    expect(screen.getByText("Use account settings")).toBeInTheDocument();
    expect(screen.getByTestId("project-orchestration-toggle")).toHaveTextContent("Customize");
    expect(screen.queryByTestId("project-orchestration-editor")).toBeNull();

    fireEvent.click(screen.getByTestId("project-orchestration-toggle"));
    expect(screen.getByTestId("project-orchestration-editor")).toBeInTheDocument();
    expect(screen.getByLabelText("About removed approval kinds")).toBeInTheDocument();
    expect(screen.getByLabelText("About approval exceptions")).toBeInTheDocument();
  });

  it("edits child_cap with no upper cap and restores all to account settings", () => {
    const spy = vi.fn();
    renderWithQuery(
      {
        child_cap: "none",
        standing_approval: { remove: ["install"], exceptions: ["server release"] },
      },
      spy,
    );
    expect(screen.getByTestId("project-orchestration-summary-chips")).toHaveTextContent(
      "Child limit: Unlimited",
    );
    expect(screen.getByTestId("project-orchestration-summary-chips")).toHaveTextContent(
      "Removed approvals: 1",
    );
    expect(screen.getByTestId("project-orchestration-toggle")).toHaveTextContent("Edit");

    fireEvent.click(screen.getByTestId("project-orchestration-toggle"));
    fireEvent.click(screen.getByTestId("project-orchestration-cap-custom"));
    fireEvent.change(screen.getByTestId("project-orchestration-cap-input"), {
      target: { value: "150" },
    });
    expect(spy).toHaveBeenLastCalledWith(expect.objectContaining({ child_cap: 150 }));

    fireEvent.click(screen.getByTestId("project-orchestration-restore-all"));
    expect(spy).toHaveBeenLastCalledWith(undefined);
    expect(screen.getByTestId("project-orchestration-summary-default")).toHaveTextContent(
      "Use account settings",
    );
  });
});
