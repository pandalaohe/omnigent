import { cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { useSessionTerminalAccess } from "./useSessionTerminalAccess";

const state = vi.hoisted(() => ({
  admin: true,
  owner: true,
  capable: true,
  online: true,
  permission: 4 as number | null,
}));
vi.mock("./useSession", () => ({
  useSession: () => ({ session: { hostId: "host-a", permissionLevel: state.permission } }),
}));
vi.mock("./useIsAdmin", () => ({ useIsAdmin: () => state.admin }));
vi.mock("./useHosts", () => ({
  useHosts: () => ({
    data: state.owner
      ? [
          {
            host_id: "host-a",
            status: state.online ? "online" : "offline",
            capabilities: state.capable ? ["terminal_elevated_v1"] : [],
          },
        ]
      : [],
  }),
}));
beforeEach(() => {
  Object.assign(state, { admin: true, owner: true, capable: true, online: true, permission: 4 });
});
afterEach(cleanup);
it("offers root only for an administrator editing a capable owned host", () => {
  expect(
    renderHook(() => useSessionTerminalAccess("session-a")).result.current.canCreateElevated,
  ).toBe(true);
});
it.each(["admin", "owner", "capable", "online"] as const)(
  "hides root when %s is unavailable",
  (key) => {
    state[key] = false;
    expect(
      renderHook(() => useSessionTerminalAccess("session-a")).result.current.canCreateElevated,
    ).toBe(false);
  },
);
it("does not offer root to a read-only session viewer, but lets the host owner revoke access", () => {
  state.permission = 1;
  const access = renderHook(() => useSessionTerminalAccess("session-a")).result.current;
  expect(access.canCreateElevated).toBe(false);
  expect(access.canManageAgentAccess).toBe(true);
});
