import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";

import { readSessionNavigationPreferences } from "@/lib/sessionNavigationPreferences";
import { MobileSessionTitleSetting, SessionNavigationSettings } from "./SessionNavigationSettings";

describe("SessionNavigationSettings", () => {
  beforeEach(() => localStorage.clear());

  it("keeps the polling window blank by default and persists positive hours", async () => {
    render(<SessionNavigationSettings />);
    const input = screen.getByLabelText("Polling window");
    expect(input).toHaveValue(null);

    fireEvent.change(input, { target: { value: "24" } });
    await waitFor(() =>
      expect(readSessionNavigationPreferences().pollingActiveWindowHours).toBe(24),
    );

    fireEvent.change(input, { target: { value: "" } });
    await waitFor(() =>
      expect(readSessionNavigationPreferences().pollingActiveWindowHours).toBeNull(),
    );
  });

  it("deprioritizes B sessions by default and lets the user disable it", async () => {
    render(<SessionNavigationSettings />);
    const toggle = screen.getByRole("switch", {
      name: "Deprioritize background sessions while polling",
    });
    expect(toggle).toBeChecked();

    fireEvent.click(toggle);
    await waitFor(() =>
      expect(readSessionNavigationPreferences().deprioritizeBackgroundSessions).toBe(false),
    );
  });

  it("opts into the mobile title layout without changing the default", async () => {
    render(<MobileSessionTitleSetting />);
    const toggle = screen.getByRole("switch", {
      name: "Show session title in the mobile top bar",
    });
    expect(toggle).not.toBeChecked();

    fireEvent.click(toggle);
    await waitFor(() =>
      expect(readSessionNavigationPreferences().nativeMobileHeaderMode).toBe("conversation-title"),
    );
  });
});
