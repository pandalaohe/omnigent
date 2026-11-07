import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

const { queuePatchMock } = vi.hoisted(() => ({ queuePatchMock: vi.fn() }));
vi.mock("@/lib/userPreferencesSync", () => ({
  queueUserPreferencePatch: queuePatchMock,
  USER_PREFERENCES_PATCH_REJECTED_EVENT: "omnigent:user-preference-rejected",
}));

import { TooltipProvider } from "@/components/ui/tooltip";
import {
  readSessionCollabPreferences,
  SESSION_COLLAB_DEFAULTS,
  writeSessionCollabPreferences,
} from "@/lib/sessionCollabPreferences";
import { ParentSessionsSettings } from "./ParentSessionsSettings";

function renderParentSettings() {
  return render(
    <TooltipProvider>
      <ParentSessionsSettings />
    </TooltipProvider>,
  );
}

afterEach(() => {
  cleanup();
  localStorage.clear();
  queuePatchMock.mockReset();
});

describe("ParentSessionsSettings", () => {
  it("loads defaults, renders hints and note, and saves without dropping sibling preferences", () => {
    writeSessionCollabPreferences({
      ...SESSION_COLLAB_DEFAULTS,
      childCap: 4,
      relayDepthMax: 12,
    });
    queuePatchMock.mockReset();

    renderParentSettings();

    const toggle = screen.getByRole("switch", { name: "Auto-rotation" });
    const compactions = screen.getByRole("spinbutton", { name: "Rotate after compactions" });
    const remaining = screen.getByRole("spinbutton", { name: "Context left at rotation" });

    expect(toggle).toHaveAttribute("aria-checked", "false");
    expect(compactions).toHaveValue(4);
    expect(remaining).toHaveValue(25);
    expect(
      screen.getByText(
        "Pi and Antigravity sessions report approximate usage; rotation still uses it.",
      ),
    ).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "About Auto-rotation" }));
    expect(
      screen.getAllByText(
        "When a top-level session's context runs low, it writes a handover note and continues in a new session. Its child sessions move with it. Off keeps today's behaviour.",
      )[0],
    ).toBeInTheDocument();

    fireEvent.click(toggle);
    fireEvent.change(compactions, { target: { value: "6" } });
    fireEvent.change(remaining, { target: { value: "30" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    expect(screen.getByRole("status")).toHaveTextContent("Saved");
    expect(readSessionCollabPreferences()).toMatchObject({
      childCap: 4,
      relayDepthMax: 12,
      autoRotationEnabled: true,
      autoRotationCompactions: 6,
      autoRotationRemainingPct: 30,
    });
    expect(queuePatchMock).toHaveBeenLastCalledWith(
      "session_collab",
      expect.objectContaining({
        childCap: 4,
        relayDepthMax: 12,
        autoRotationEnabled: true,
        autoRotationCompactions: 6,
        autoRotationRemainingPct: 30,
      }),
    );
  });

  it("preserves unsaved drafts across unrelated storage events and validates bounds", () => {
    renderParentSettings();

    const compactions = screen.getByRole("spinbutton", { name: "Rotate after compactions" });
    const remaining = screen.getByRole("spinbutton", { name: "Context left at rotation" });

    fireEvent.change(compactions, { target: { value: "12" } });
    fireEvent(window, new StorageEvent("storage", { key: "theme" }));
    expect(compactions).toHaveValue(12);

    for (const badCompactions of ["0", "21", ""]) {
      fireEvent.change(compactions, { target: { value: badCompactions } });
      fireEvent.click(screen.getByRole("button", { name: "Save" }));
      expect(screen.getByRole("alert")).toHaveTextContent(
        "Enter a whole number of compactions from 1 to 20.",
      );
      expect(queuePatchMock).not.toHaveBeenCalled();
    }

    fireEvent.change(compactions, { target: { value: "5" } });
    for (const badRemaining of ["0", "91", ""]) {
      fireEvent.change(remaining, { target: { value: badRemaining } });
      fireEvent.click(screen.getByRole("button", { name: "Save" }));
      expect(screen.getByRole("alert")).toHaveTextContent("Enter a whole percentage from 1 to 90.");
      expect(queuePatchMock).not.toHaveBeenCalled();
    }
  });

  it("reports server rejection and refreshes a clean draft on rollback", async () => {
    renderParentSettings();

    const compactions = screen.getByRole("spinbutton", { name: "Rotate after compactions" });
    fireEvent.change(compactions, { target: { value: "7" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(screen.getByRole("status")).toHaveTextContent("Saved");

    fireEvent(
      window,
      new CustomEvent("omnigent:user-preference-rejected", {
        detail: { namespace: "session_collab", status: 422 },
      }),
    );
    expect(screen.getByRole("alert")).toHaveTextContent(
      "The server rejected these parent session settings (HTTP 422).",
    );
    expect(screen.queryByRole("status")).toBeNull();

    act(() => {
      writeSessionCollabPreferences({
        ...readSessionCollabPreferences(),
        autoRotationCompactions: 4,
      });
    });
    await waitFor(() => expect(compactions).toHaveValue(4));
  });
});
