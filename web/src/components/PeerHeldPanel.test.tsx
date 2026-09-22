import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useSession } from "@/hooks/useSession";
import { actOnPeerMessage, listPeerMessages, updateSession } from "@/lib/sessionsApi";
import type { PeerMessageRecord } from "@/lib/sessionsApi";
import type { Session } from "@/lib/types";
import { PeerHeldPanel } from "./PeerHeldPanel";

vi.mock("@/hooks/useSession", () => ({
  useSession: vi.fn(),
}));
vi.mock("@/lib/sessionsApi", () => ({
  listPeerMessages: vi.fn(),
  actOnPeerMessage: vi.fn(),
  updateSession: vi.fn(),
}));

const useSessionMock = vi.mocked(useSession);
const listPeerMessagesMock = vi.mocked(listPeerMessages);
const actOnPeerMessageMock = vi.mocked(actOnPeerMessage);
const updateSessionMock = vi.mocked(updateSession);

function record(overrides: Partial<PeerMessageRecord> = {}): PeerMessageRecord {
  return {
    id: "peer_abc123",
    senderSessionId: "conv_sender01",
    receiverSessionId: "conv_receiver",
    correlationId: "corr-1",
    ref: "corr-1",
    text: "hello",
    state: "held",
    reason: null,
    createdAtS: Math.floor(Date.now() / 1000) - 60,
    updatedAtS: Math.floor(Date.now() / 1000) - 60,
    expiresAtS: Math.floor(Date.now() / 1000) + 3600,
    replyPeerId: null,
    repliedAtS: null,
    ...overrides,
  };
}

function sessionWith(labels: Record<string, string> = {}): Session {
  return {
    id: "conv_receiver",
    agentId: "ag",
    agentName: "dev",
    runnerId: null,
    status: "idle",
    createdAt: 1_700_000_000,
    title: "s",
    labels,
    items: [],
    pendingElicitations: [],
    permissionLevel: 3,
    parentSessionId: null,
    subAgentName: null,
    kind: "default",
  } as Session;
}

beforeEach(() => {
  useSessionMock.mockReset();
  listPeerMessagesMock.mockReset();
  actOnPeerMessageMock.mockReset();
  updateSessionMock.mockReset();
  useSessionMock.mockReturnValue({ session: sessionWith(), isLoading: false, error: null });
  listPeerMessagesMock.mockResolvedValue([]);
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("PeerHeldPanel", () => {
  it("fetches and lists held/pending/queued records for the session", async () => {
    listPeerMessagesMock.mockResolvedValue([
      record({ id: "peer_1", state: "held" }),
      record({ id: "peer_2", state: "pending", senderSessionId: "conv_sender02" }),
    ]);

    render(<PeerHeldPanel sessionId="conv_receiver" open onOpenChange={() => {}} />);

    expect(await screen.findAllByTestId("peer-held-row")).toHaveLength(2);
    expect(listPeerMessagesMock).toHaveBeenCalledWith("conv_receiver", ["held", "pending", "queued"]);
    expect(screen.getByText("Held")).toBeInTheDocument();
    expect(screen.getByText("Pending")).toBeInTheDocument();
    // "From <short id> · ref=<ref>" splits across sibling text nodes
    // (interpolated JSX), so match on the row's full text content. The
    // Dialog portals into document.body, outside RTL's `container`.
    const row1 = document.querySelector('[data-peer-id="peer_1"]');
    expect(row1).toHaveTextContent("From conv_sen · ref=corr-1");
  });

  it("does not fetch while closed", () => {
    render(<PeerHeldPanel sessionId="conv_receiver" open={false} onOpenChange={() => {}} />);
    expect(listPeerMessagesMock).not.toHaveBeenCalled();
  });

  it("releases a record: calls the action route and drops the row optimistically", async () => {
    listPeerMessagesMock.mockResolvedValue([record({ id: "peer_1" })]);
    actOnPeerMessageMock.mockResolvedValue(record({ id: "peer_1", state: "pending" }));

    render(<PeerHeldPanel sessionId="conv_receiver" open onOpenChange={() => {}} />);
    await screen.findByTestId("peer-held-row");

    fireEvent.click(screen.getByRole("button", { name: /release/i }));

    expect(screen.queryByTestId("peer-held-row")).not.toBeInTheDocument();
    await waitFor(() =>
      expect(actOnPeerMessageMock).toHaveBeenCalledWith("conv_receiver", "peer_1", "release"),
    );
  });

  it("refuses a record: calls the action route with refuse", async () => {
    listPeerMessagesMock.mockResolvedValue([record({ id: "peer_1" })]);
    actOnPeerMessageMock.mockResolvedValue(record({ id: "peer_1", state: "refused_by_user" }));

    render(<PeerHeldPanel sessionId="conv_receiver" open onOpenChange={() => {}} />);
    await screen.findByTestId("peer-held-row");

    fireEvent.click(screen.getByRole("button", { name: /refuse/i }));

    await waitFor(() =>
      expect(actOnPeerMessageMock).toHaveBeenCalledWith("conv_receiver", "peer_1", "refuse"),
    );
  });

  it("refetches the list when the action route 409s (already resolved elsewhere)", async () => {
    listPeerMessagesMock
      .mockResolvedValueOnce([record({ id: "peer_1" })])
      .mockResolvedValueOnce([]);
    actOnPeerMessageMock.mockRejectedValue(Object.assign(new Error("conflict"), { status: 409 }));

    render(<PeerHeldPanel sessionId="conv_receiver" open onOpenChange={() => {}} />);
    await screen.findByTestId("peer-held-row");

    fireEvent.click(screen.getByRole("button", { name: /release/i }));

    await waitFor(() => expect(listPeerMessagesMock).toHaveBeenCalledTimes(2));
  });

  it("shows the session's current peer_inbound policy, defaulting to accept", async () => {
    useSessionMock.mockReturnValue({
      session: sessionWith({ peer_inbound: "hold" }),
      isLoading: false,
      error: null,
    });
    render(<PeerHeldPanel sessionId="conv_receiver" open onOpenChange={() => {}} />);
    expect(await screen.findByTestId("peer-inbound-policy")).toHaveTextContent("Hold");
  });

  it("writes the policy label, merged with existing labels, on selection", async () => {
    useSessionMock.mockReturnValue({
      session: sessionWith({ some_other_label: "x" }),
      isLoading: false,
      error: null,
    });
    updateSessionMock.mockResolvedValue(sessionWith({ some_other_label: "x", peer_inbound: "refuse" }));

    render(<PeerHeldPanel sessionId="conv_receiver" open onOpenChange={() => {}} />);
    const trigger = await screen.findByTestId("peer-inbound-policy");
    trigger.focus();
    fireEvent.keyDown(trigger, { key: "Enter" });
    const listbox = await screen.findByRole("listbox");
    fireEvent.click(within(listbox).getByRole("option", { name: "Refuse" }));

    await waitFor(() =>
      expect(updateSessionMock).toHaveBeenCalledWith("conv_receiver", {
        labels: { some_other_label: "x", peer_inbound: "refuse" },
      }),
    );
  });

  it("polls every 5s while open", async () => {
    vi.useFakeTimers();
    render(<PeerHeldPanel sessionId="conv_receiver" open onOpenChange={() => {}} />);
    await act(async () => {
      await Promise.resolve();
    });
    expect(listPeerMessagesMock).toHaveBeenCalledTimes(1);

    await act(async () => {
      vi.advanceTimersByTime(5000);
      await Promise.resolve();
    });
    expect(listPeerMessagesMock).toHaveBeenCalledTimes(2);

    await act(async () => {
      vi.advanceTimersByTime(5000);
      await Promise.resolve();
    });
    expect(listPeerMessagesMock).toHaveBeenCalledTimes(3);
  });
});
