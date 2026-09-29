// The notice diff: only finding ids that were absent from the previous
// summary notify, the first summary only seeds the baseline, and the
// permission gate matches useIdleNotifications.

import { cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const summary = vi.hoisted(() => ({ current: undefined as unknown }));

vi.mock("@/hooks/useSystemStatus", () => ({
  useSystemStatusSummary: () => ({ data: summary.current }),
}));

const notifications = vi.hoisted(() => ({
  getNotificationPermission: vi.fn(),
  showNotification: vi.fn(),
}));

const navigateMock = vi.hoisted(() => vi.fn());

vi.mock("@/lib/browserNotifications", () => notifications);
vi.mock("@/lib/nativeBridge", () => ({ isNativeShell: () => false }));
vi.mock("@/lib/routing", () => ({ useNavigate: () => navigateMock }));

import { useSystemStatusNotifications } from "./useSystemStatusNotifications";

function finding(id: string, level: "amber" | "red" = "amber") {
  return {
    id,
    target: "host_1",
    kind: "cpu",
    level,
    since: 0,
    detail: `${id} above threshold`,
    top_session: null,
  };
}

function setFindings(findings: ReturnType<typeof finding>[]) {
  summary.current = { revision: 1, level: findings.length > 0 ? "amber" : "ok", findings };
}

beforeEach(() => {
  summary.current = undefined;
  notifications.getNotificationPermission.mockReset();
  notifications.showNotification.mockReset();
  notifications.getNotificationPermission.mockReturnValue("granted");
  navigateMock.mockReset();
});

afterEach(cleanup);

describe("useSystemStatusNotifications", () => {
  it("seeds the first summary without notifying", () => {
    // The finding is already present when the page loads: seeding, not news.
    setFindings([finding("host_1:cpu")]);
    const { rerender } = renderHook(() => useSystemStatusNotifications());
    rerender();

    expect(notifications.showNotification).not.toHaveBeenCalled();
  });

  it("notifies once for a new finding id and not again for the same id", () => {
    setFindings([]);
    const { rerender } = renderHook(() => useSystemStatusNotifications());
    setFindings([finding("host_1:cpu")]);
    rerender();

    expect(notifications.showNotification).toHaveBeenCalledTimes(1);
    expect(notifications.showNotification).toHaveBeenCalledWith(
      expect.objectContaining({
        tag: "omnigent:system:host_1:cpu",
        navigatePath: "/system",
        body: "host_1:cpu above threshold",
      }),
    );

    rerender();
    expect(notifications.showNotification).toHaveBeenCalledTimes(1);
  });

  it("notifies when a finding is replaced by another kind at the same level", () => {
    setFindings([]);
    const { rerender } = renderHook(() => useSystemStatusNotifications());
    setFindings([finding("host_1:cpu")]);
    rerender();
    expect(notifications.showNotification).toHaveBeenCalledTimes(1);

    // CPU clears, disk appears: a different id, same amber level.
    setFindings([{ ...finding("host_1:disk"), kind: "disk", detail: "disk above threshold" }]);
    rerender();
    expect(notifications.showNotification).toHaveBeenCalledTimes(2);
    expect(notifications.showNotification).toHaveBeenLastCalledWith(
      expect.objectContaining({ tag: "omnigent:system:host_1:disk" }),
    );
  });

  it("stays silent without permission", () => {
    notifications.getNotificationPermission.mockReturnValue("default");
    setFindings([]);
    const { rerender } = renderHook(() => useSystemStatusNotifications());
    setFindings([finding("host_1:cpu", "red")]);
    rerender();

    expect(notifications.showNotification).not.toHaveBeenCalled();
  });

  it("navigates to /system when the notification is clicked", () => {
    setFindings([]);
    const { rerender } = renderHook(() => useSystemStatusNotifications());
    setFindings([finding("host_1:cpu")]);
    rerender();

    const shown = notifications.showNotification.mock.calls[0][0] as {
      onClick?: () => void;
      navigatePath?: string;
    };
    expect(shown.navigatePath).toBe("/system");
    shown.onClick?.();
    expect(navigateMock).toHaveBeenCalledWith("/system");
  });
});
