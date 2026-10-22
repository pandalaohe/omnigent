import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { ConnectedAppsSettings } from "./ConnectedAppsSettings";

const api = vi.hoisted(() => ({
  list: vi.fn(),
  revoke: vi.fn(),
  receipt: vi.fn(),
  identity: { user: "alice", generation: 1, server: "server" },
}));

vi.mock("@/hooks/useViewerId", () => ({ useViewerId: () => api.identity.user }));
vi.mock("@/lib/identity", () => ({ getCurrentUserId: () => api.identity.user }));
vi.mock("@/lib/host", () => ({
  getOmnigentServerIdentity: () => api.identity.server,
  getOmnigentHostGeneration: () => api.identity.generation,
}));

vi.mock("@/lib/browserAnnotationsApi", () => ({
  getConnectedApps: api.list,
  revokeConnectedApp: api.revoke,
  getOldAnnotationReceipt: api.receipt,
}));

beforeEach(() => {
  api.list.mockReset().mockResolvedValue([]);
  api.revoke.mockReset();
  api.receipt.mockReset();
  api.identity.user = "alice";
  api.identity.generation = 1;
  api.identity.server = "server";
  window.__OMNIGENT_BASE_PATH__ = "";
});

afterEach(() => {
  delete window.__OMNIGENT_BASE_PATH__;
});

const recordedReceipt = {
  batch_id: "b".repeat(32),
  state: "accepted",
  session_id: "actual/target",
  dispatch_phase: "confirmed",
  retry_required: false,
  item_id: "item-1",
  pending_id: "pending-1",
  accepted_at: 1_800_000_000,
  session_path: "/sessions/wrong-target",
} as const;

function checkBatch(grantId = "old_grant-1", batchId = "b".repeat(32)) {
  fireEvent.change(screen.getByLabelText("Original grant ID"), { target: { value: grantId } });
  fireEvent.change(screen.getByLabelText("Batch ID"), { target: { value: batchId } });
  fireEvent.click(screen.getByRole("button", { name: "Check batch" }));
}

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

it("shows the actual receiving session under the same base path and keeps the check after revoke", async () => {
  const grant = {
    grant_id: "old_grant-1",
    client_name: "omnigent-chrome",
    label: "Work Chrome",
    scope: "annotations:write",
    created_at: 1_800_000_000,
    expires_at: null,
    last_used_at: null,
  };
  api.list.mockResolvedValueOnce([grant]).mockResolvedValueOnce([]);
  api.revoke.mockResolvedValue(undefined);
  api.receipt.mockResolvedValue(recordedReceipt);
  window.__OMNIGENT_BASE_PATH__ = "/proxy/6767";
  render(<ConnectedAppsSettings pairingAvailable />);
  expect(await screen.findByText("Work Chrome")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Connect browser extension" })).toBeDisabled();
  fireEvent.change(screen.getByLabelText("Browser extension code"), {
    target: { value: "ABCD-2345" },
  });
  expect(screen.getByRole("button", { name: "Connect browser extension" })).toBeEnabled();
  checkBatch();
  const result = await screen.findByRole("status");
  expect(within(result).getByText("Batch accepted")).toBeInTheDocument();
  expect(within(result).getByText("old_grant-1")).toBeInTheDocument();
  expect(within(result).getByText("b".repeat(32))).toBeInTheDocument();
  expect(within(result).getByRole("link", { name: "actual/target" })).toHaveAttribute(
    "href",
    "/proxy/6767/c/actual%2Ftarget",
  );
  expect(result).toHaveTextContent("This does not confirm that the agent read it.");
  expect(api.receipt).toHaveBeenCalledWith("old_grant-1", "b".repeat(32));
  fireEvent.click(screen.getByRole("button", { name: "Revoke Work Chrome" }));
  await waitFor(() => expect(screen.queryByText("Work Chrome")).not.toBeInTheDocument());
  expect(screen.getByRole("status")).toHaveTextContent("Batch accepted");
});

it.each([
  [
    { batch_id: "b".repeat(32), state: "not_admitted" },
    "Not admitted",
    "You can explicitly confirm migration of this batch in the extension.",
  ],
  [
    { batch_id: "b".repeat(32), state: "unknown_record" },
    "Still unconfirmed",
    "This result does not establish that migration or resending is safe.",
  ],
  [
    { ...recordedReceipt, dispatch_phase: "ready", retry_required: true },
    "Delivery paused",
    "requires an explicit retry in the extension",
  ],
  [
    { ...recordedReceipt, dispatch_phase: "ready" },
    "Batch accepted",
    "delivery has not been confirmed",
  ],
  [
    { ...recordedReceipt, state: "delivery_unknown", dispatch_phase: "dispatching" },
    "Delivery unknown",
    "delivery could not be confirmed",
  ],
  [
    { ...recordedReceipt, dispatch_phase: "uncertain" },
    "Delivery unknown",
    "delivery could not be confirmed",
  ],
])("distinguishes recovery outcome %s", async (receipt, title, detail) => {
  api.receipt.mockResolvedValue(receipt);
  render(<ConnectedAppsSettings pairingAvailable />);
  checkBatch();
  const result = await screen.findByRole("status");
  expect(result).toHaveTextContent(title as string);
  expect(result).toHaveTextContent(detail as string);
  expect(screen.queryByRole("button", { name: /retry|send|migrate/i })).not.toBeInTheDocument();
});

it.each([
  ["../invalid", "b".repeat(32)],
  ["valid", "B".repeat(32)],
  ["valid", "short"],
  ["", "b".repeat(32)],
  ["g".repeat(129), "b".repeat(32)],
])("rejects invalid recovery IDs %s / %s without a request", async (grantId, batchId) => {
  render(<ConnectedAppsSettings pairingAvailable />);
  checkBatch(grantId, batchId);
  expect(await screen.findByRole("alert")).toHaveTextContent("Enter a valid original grant ID");
  expect(api.receipt).not.toHaveBeenCalled();
});

it("shows the same generic check error without exposing the server response", async () => {
  api.receipt.mockRejectedValue(new Error("private existence or permission detail"));
  render(<ConnectedAppsSettings pairingAvailable />);
  checkBatch();
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "This batch could not be checked. Verify the IDs and try again.",
  );
  expect(screen.queryByText(/private existence/)).not.toBeInTheDocument();
});

it.each(["account", "server", "generation"])(
  "clears recovery input and ignores a pending result after a %s change",
  async (change) => {
    let resolveOld!: (value: unknown) => void;
    api.receipt.mockReturnValueOnce(
      new Promise((resolve) => {
        resolveOld = resolve;
      }),
    );
    const view = render(<ConnectedAppsSettings pairingAvailable />);
    checkBatch();
    expect(api.receipt).toHaveBeenCalledTimes(1);
    if (change === "account") api.identity.user = "bob";
    else if (change === "server") api.identity.server = "other-server";
    else api.identity.generation = 2;
    view.rerender(<ConnectedAppsSettings pairingAvailable />);
    expect(screen.getByLabelText("Original grant ID")).toHaveValue("");
    expect(screen.getByLabelText("Batch ID")).toHaveValue("");
    await act(async () => resolveOld(recordedReceipt));
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Check batch" })).toBeEnabled();
  },
);

it("ignores an older response after the inputs start another check", async () => {
  let resolveOld!: (value: unknown) => void;
  api.receipt
    .mockReturnValueOnce(
      new Promise((resolve) => {
        resolveOld = resolve;
      }),
    )
    .mockResolvedValueOnce({ batch_id: "c".repeat(32), state: "unknown_record" });
  render(<ConnectedAppsSettings pairingAvailable />);
  checkBatch();
  checkBatch("next-grant", "c".repeat(32));
  expect(await screen.findByRole("status")).toHaveTextContent("Still unconfirmed");
  await act(async () => resolveOld(recordedReceipt));
  expect(screen.getByRole("status")).toHaveTextContent("next-grant");
  expect(screen.getByRole("status")).not.toHaveTextContent("Batch accepted");
});

it("ignores a pending failure after unmount", async () => {
  let rejectOld!: (reason: Error) => void;
  api.receipt.mockReturnValueOnce(
    new Promise((_resolve, reject) => {
      rejectOld = reject;
    }),
  );
  const view = render(<ConnectedAppsSettings pairingAvailable />);
  checkBatch();
  view.unmount();
  await act(async () => rejectOld(new Error("old request")));
  expect(api.receipt).toHaveBeenCalledTimes(1);
});
