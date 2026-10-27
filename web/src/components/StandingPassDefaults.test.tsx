import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  readSessionCollabPreferences,
  writeSessionCollabPreferences,
} from "@/lib/sessionCollabPreferences";
import { StandingPassDefaults } from "./StandingPassDefaults";

vi.mock("@/lib/userPreferencesSync", () => ({ queueUserPreferencePatch: vi.fn() }));
afterEach(() => {
  cleanup();
  localStorage.clear();
});

describe("standing pass defaults", () => {
  it("starts empty and preserves other collaboration preferences", () => {
    writeSessionCollabPreferences({ ...readSessionCollabPreferences(), childCap: 3 });
    render(<StandingPassDefaults />);
    expect(screen.getByRole("checkbox", { name: "Install" })).not.toBeChecked();
    fireEvent.click(screen.getByRole("checkbox", { name: "Install" }));
    expect(readSessionCollabPreferences()).toMatchObject({
      standingPassDefault: ["install"],
      childCap: 3,
    });
    fireEvent.click(screen.getByRole("checkbox", { name: "Install" }));
    expect(readSessionCollabPreferences().standingPassDefault).toEqual([]);
  });

  it("adds and removes custom kinds while preserving built-ins", () => {
    render(<StandingPassDefaults />);
    fireEvent.click(screen.getByRole("checkbox", { name: "Merge" }));
    fireEvent.change(screen.getByLabelText("Extra standing pass kind"), {
      target: { value: "  local   preview " },
    });
    fireEvent.click(screen.getByRole("button", { name: "Add kind" }));
    expect(readSessionCollabPreferences().standingPassDefault).toEqual(["merge", "local preview"]);
    fireEvent.click(screen.getByRole("button", { name: "Remove local preview" }));
    expect(readSessionCollabPreferences().standingPassDefault).toEqual(["merge"]);
  });
});
