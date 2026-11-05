import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  readSessionCollabPreferences,
  writeSessionCollabPreferences,
} from "@/lib/sessionCollabPreferences";
import { StandingApprovalDefaults } from "./StandingApprovalDefaults";

vi.mock("@/lib/userPreferencesSync", () => ({ queueUserPreferencePatch: vi.fn() }));
afterEach(() => {
  cleanup();
  localStorage.clear();
});

describe("standing approval defaults", () => {
  it("starts empty and preserves other collaboration preferences", () => {
    writeSessionCollabPreferences({ ...readSessionCollabPreferences(), childCap: 3 });
    render(<StandingApprovalDefaults />);
    expect(screen.getByRole("button", { name: "Install" })).toHaveAttribute(
      "aria-pressed",
      "false",
    );
    expect(screen.getByRole("button", { name: "Answer child approvals" })).toHaveAttribute(
      "aria-pressed",
      "false",
    );
    fireEvent.click(screen.getByRole("button", { name: "Install" }));
    expect(readSessionCollabPreferences()).toMatchObject({
      standingPassDefault: ["install"],
      childCap: 3,
    });
    expect(screen.getByRole("button", { name: "Install" })).toHaveAttribute("aria-pressed", "true");
    fireEvent.click(screen.getByRole("button", { name: "Install" }));
    expect(readSessionCollabPreferences().standingPassDefault).toEqual([]);
    fireEvent.click(screen.getByRole("button", { name: "Answer child approvals" }));
    expect(readSessionCollabPreferences().standingPassDefault).toEqual(["answer-child-approvals"]);
  });

  it("adds and removes custom kinds while preserving built-ins", () => {
    render(<StandingApprovalDefaults />);
    fireEvent.click(screen.getByRole("button", { name: "Merge" }));
    fireEvent.change(screen.getByLabelText("Another action"), {
      target: { value: "  local   preview " },
    });
    fireEvent.click(screen.getByRole("button", { name: "Add kind" }));
    expect(readSessionCollabPreferences().standingPassDefault).toEqual(["merge", "local preview"]);
    fireEvent.click(screen.getByRole("button", { name: "Remove local preview" }));
    expect(readSessionCollabPreferences().standingPassDefault).toEqual(["merge"]);
  });
});
