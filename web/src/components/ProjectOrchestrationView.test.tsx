import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { ProjectOrchestrationView } from "./ProjectOrchestrationView";

afterEach(cleanup);
describe("project orchestration view", () => {
  it("distinguishes inherited defaults from an unlimited override", () => {
    const { rerender } = render(<ProjectOrchestrationView orchestration={undefined} />);
    expect(screen.getByText("Use account default")).toBeInTheDocument();
    expect(screen.queryByText("Setup")).toBeNull();
    rerender(
      <ProjectOrchestrationView
        orchestration={{
          child_cap: "none",
          standing_pass: { remove: ["install"], exceptions: ["server release"] },
        }}
      />,
    );
    expect(screen.getByText("Unlimited")).toBeInTheDocument();
    expect(screen.getByText("install")).toBeInTheDocument();
    expect(screen.getByText("server release")).toBeInTheDocument();
    expect(screen.queryByRole("textbox")).toBeNull();
  });
});
