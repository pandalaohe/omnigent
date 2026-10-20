import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

const { queuePatchMock } = vi.hoisted(() => ({ queuePatchMock: vi.fn() }));
vi.mock("@/lib/userPreferencesSync", () => ({
  queueUserPreferencePatch: queuePatchMock,
  USER_PREFERENCES_PATCH_REJECTED_EVENT: "omnigent:user-preference-rejected",
}));

import { ChildSessionsPanel } from "./ChildSessionsPanel";
import {
  readSessionCollabPreferences,
  SESSION_COLLAB_DEFAULTS,
  writeSessionCollabPreferences,
} from "@/lib/sessionCollabPreferences";

afterEach(() => {
  cleanup();
  localStorage.clear();
  queuePatchMock.mockReset();
});

describe("ChildSessionsPanel", () => {
  it("saves child controls without dropping existing collaboration preferences", () => {
    writeSessionCollabPreferences({ ...SESSION_COLLAB_DEFAULTS, relayDepthMax: 3 });
    render(<ChildSessionsPanel />);
    fireEvent.change(screen.getByRole("textbox", { name: /Child session limit/ }), {
      target: { value: "4" },
    });
    fireEvent.change(screen.getByRole("textbox", { name: /Tight below/ }), {
      target: { value: "45.5" },
    });
    fireEvent.change(screen.getByRole("textbox", { name: /Low below/ }), {
      target: { value: "15" },
    });
    fireEvent.change(screen.getByRole("textbox", { name: /Remaining quota floor/ }), {
      target: { value: "0" },
    });
    fireEvent.change(screen.getByRole("textbox", { name: /Claude.*period/ }), {
      target: { value: "5h" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save child settings" }));

    expect(readSessionCollabPreferences()).toMatchObject({
      relayDepthMax: 3,
      childCap: 4,
      quotaTightBelowPct: 45.5,
      quotaLowBelowPct: 15,
      quotaFloorPct: 0,
      quotaPeriods: { claude: "5h" },
    });
    expect(queuePatchMock).toHaveBeenLastCalledWith(
      "session_collab",
      expect.objectContaining({ relayDepthMax: 3, childCap: 4 }),
    );
    expect(screen.getByRole("status")).toHaveTextContent("Saved preferences.");
  });

  it("keeps invalid thresholds in the draft until corrected", () => {
    render(<ChildSessionsPanel />);
    fireEvent.change(screen.getByRole("textbox", { name: /Low below/ }), {
      target: { value: "60" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save child settings" }));
    expect(screen.getByRole("alert")).toHaveTextContent("0 < low < tight");
    expect(readSessionCollabPreferences()).toEqual(SESSION_COLLAB_DEFAULTS);
    expect(queuePatchMock).not.toHaveBeenCalled();

    fireEvent.change(screen.getByRole("textbox", { name: /Low below/ }), {
      target: { value: "10" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save child settings" }));
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(readSessionCollabPreferences().quotaLowBelowPct).toBe(10);
  });

  it("reports a server rejection after a local save", () => {
    render(<ChildSessionsPanel />);
    fireEvent.change(screen.getByRole("textbox", { name: /Child session limit/ }), {
      target: { value: "2" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save child settings" }));
    fireEvent(
      window,
      new CustomEvent("omnigent:user-preference-rejected", {
        detail: { namespace: "session_collab", status: 422 },
      }),
    );
    expect(screen.getByRole("alert")).toHaveTextContent("server rejected");
  });
});
