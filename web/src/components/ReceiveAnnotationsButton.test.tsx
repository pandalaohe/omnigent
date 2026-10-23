import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import { ReceiveAnnotationsButton } from "./ReceiveAnnotationsButton";

const api = vi.hoisted(() => ({
  get: vi.fn(),
  set: vi.fn(),
  identity: { user: "alice", generation: 1 },
}));

vi.mock("@/hooks/useViewerId", () => ({ useViewerId: () => api.identity.user }));
vi.mock("@/lib/identity", () => ({ getCurrentUserId: () => api.identity.user }));
vi.mock("@/lib/host", () => ({
  getOmnigentServerIdentity: () => "server",
  getOmnigentHostGeneration: () => api.identity.generation,
}));

vi.mock("@/lib/browserAnnotationsApi", () => ({
  getAnnotationReceiver: api.get,
  setAnnotationReceiver: api.set,
  BrowserAnnotationsApiError: class extends Error {},
}));

beforeEach(() => {
  api.get.mockReset();
  api.set.mockReset();
  api.identity.user = "alice";
  api.identity.generation = 1;
});

it("ignores an old session's late load and mutation after navigation", async () => {
  const first = "a".repeat(32);
  const second = "b".repeat(32);
  let resolveOldLoad!: (value: unknown) => void;
  const oldLoad = new Promise((resolve) => {
    resolveOldLoad = resolve;
  });
  api.get.mockReturnValueOnce(oldLoad).mockResolvedValue({
    session_id: null,
    revision: 4,
    target_editable: false,
  });
  const view = render(<ReceiveAnnotationsButton sessionId={first} archived={false} />);
  view.rerender(<ReceiveAnnotationsButton sessionId={second} archived={false} />);
  await waitFor(() => expect(api.get).toHaveBeenCalledWith(second));
  await act(async () => resolveOldLoad({ session_id: first, revision: 3, target_editable: true }));
  expect(screen.getByRole("button", { name: "Receive web annotations" })).toHaveAttribute(
    "title",
    "You need an explicit edit grant for this session.",
  );

  api.get.mockResolvedValue({ session_id: null, revision: 5, target_editable: true });
  fireEvent(window, new Event("focus"));
  await waitFor(() => expect(screen.getByRole("button")).toBeEnabled());
  let rejectMutation!: (reason: Error) => void;
  api.set.mockReturnValueOnce(
    new Promise((_resolve, reject) => {
      rejectMutation = reject;
    }),
  );
  fireEvent.click(screen.getByRole("button"));
  view.rerender(<ReceiveAnnotationsButton sessionId={first} archived={false} />);
  await waitFor(() => expect(screen.getByRole("button")).toBeEnabled());
  await act(async () => rejectMutation(new Error("old request")));
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
});

it("arms the selected session with the server revision and shows pressed state", async () => {
  const updated = { session_id: "a".repeat(32), revision: 8, target_editable: true };
  api.get
    .mockResolvedValueOnce({ session_id: null, revision: 7, target_editable: true })
    .mockResolvedValue(updated);
  api.set.mockResolvedValue(updated);
  render(<ReceiveAnnotationsButton sessionId={"a".repeat(32)} archived={false} />);

  const button = await screen.findByRole("button", { name: "Receive web annotations" });
  await waitFor(() => expect(button).toBeEnabled());
  fireEvent.click(button);

  await waitFor(() => expect(api.set).toHaveBeenCalledWith("a".repeat(32), 7));
  await waitFor(() =>
    expect(screen.getByRole("button", { name: "Receiving web annotations" })).toHaveAttribute(
      "aria-pressed",
      "true",
    ),
  );
});

it("keeps noneditable sessions visible with a disabled reason", async () => {
  api.get.mockResolvedValue({ session_id: null, revision: 0, target_editable: false });
  render(<ReceiveAnnotationsButton sessionId={"b".repeat(32)} archived={false} />);

  const button = await screen.findByRole("button", { name: "Receive web annotations" });
  await waitFor(() =>
    expect(button).toHaveAttribute("title", "You need an explicit edit grant for this session."),
  );
  expect(button).toBeDisabled();
  fireEvent.click(button);
  expect(api.set).not.toHaveBeenCalled();
});
