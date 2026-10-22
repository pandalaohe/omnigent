import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import { ConnectedAppsSettings } from "./ConnectedAppsSettings";

const api = vi.hoisted(() => ({
  list: vi.fn(),
  revoke: vi.fn(),
  identity: { user: "alice", generation: 1 },
}));

vi.mock("@/hooks/useViewerId", () => ({ useViewerId: () => api.identity.user }));
vi.mock("@/lib/identity", () => ({ getCurrentUserId: () => api.identity.user }));
vi.mock("@/lib/host", () => ({
  getOmnigentServerIdentity: () => "server",
  getOmnigentHostGeneration: () => api.identity.generation,
}));

vi.mock("@/lib/browserAnnotationsApi", () => ({
  getConnectedApps: api.list,
  revokeConnectedApp: api.revoke,
}));

beforeEach(() => {
  api.list.mockReset();
  api.revoke.mockReset();
  api.identity.user = "alice";
  api.identity.generation = 1;
});

it("drops old account results and cannot revoke an old account's grant", async () => {
  const oldGrant = {
    grant_id: "old",
    client_name: "browser",
    label: "Old browser",
    scope: "annotations:write",
    created_at: 1_800_000_000,
    expires_at: null,
    last_used_at: null,
  };
  let resolveOld!: (value: unknown) => void;
  api.list
    .mockReturnValueOnce(
      new Promise((resolve) => {
        resolveOld = resolve;
      }),
    )
    .mockResolvedValueOnce([]);
  const view = render(<ConnectedAppsSettings />);
  api.identity.user = "bob";
  api.identity.generation = 2;
  view.rerender(<ConnectedAppsSettings />);
  await waitFor(() => expect(screen.getByText("No connected apps.")).toBeInTheDocument());
  await act(async () => resolveOld([oldGrant]));
  expect(screen.queryByText("Old browser")).not.toBeInTheDocument();
  expect(api.revoke).not.toHaveBeenCalled();
});

it("does not refresh or show a revoke error after a connection switch", async () => {
  const grant = {
    grant_id: "old",
    client_name: "browser",
    label: "Old browser",
    scope: "annotations:write",
    created_at: 1_800_000_000,
    expires_at: null,
    last_used_at: null,
  };
  let rejectOld!: (reason: Error) => void;
  api.list.mockResolvedValueOnce([grant]).mockResolvedValueOnce([]);
  api.revoke.mockReturnValueOnce(
    new Promise((_resolve, reject) => {
      rejectOld = reject;
    }),
  );
  const view = render(<ConnectedAppsSettings />);
  fireEvent.click(await screen.findByRole("button", { name: "Revoke Old browser" }));
  api.identity.generation = 2;
  view.rerender(<ConnectedAppsSettings />);
  await waitFor(() => expect(screen.getByText("No connected apps.")).toBeInTheDocument());
  await act(async () => rejectOld(new Error("old connection")));
  expect(api.list).toHaveBeenCalledTimes(2);
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Revoke Old browser" })).not.toBeInTheDocument();
});

it("shows the individual app grant and revokes only the chosen one", async () => {
  const browser = {
    grant_id: "browser-1",
    client_name: "omnigent-chrome",
    label: "Work Chrome",
    scope: "annotations:write",
    created_at: 1_800_000_000,
    expires_at: 1_800_100_000,
    last_used_at: null,
  };
  const slack = {
    ...browser,
    grant_id: "slack-2",
    client_name: "slack",
    label: "Slack",
    scope: "sessions",
  };
  api.list.mockResolvedValueOnce([browser, slack]).mockResolvedValueOnce([slack]);
  api.revoke.mockResolvedValue(undefined);
  render(<ConnectedAppsSettings />);

  expect(await screen.findByText("Work Chrome")).toBeInTheDocument();
  expect(screen.getByText("Web annotations", { exact: false })).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Revoke Work Chrome" }));

  await waitFor(() => expect(api.revoke).toHaveBeenCalledWith("browser-1"));
  await waitFor(() => expect(screen.queryByText("Work Chrome")).not.toBeInTheDocument());
  expect(screen.getByText("Slack")).toBeInTheDocument();
});
