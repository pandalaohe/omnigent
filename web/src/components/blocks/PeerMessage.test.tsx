import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import type { ParsedPeerMessage } from "@/lib/peerMessage";
import { PeerMessageView } from "./PeerMessage";

afterEach(cleanup);

const handoffBrief: ParsedPeerMessage = {
  senderId: "a".repeat(32),
  title: "Fix the flaky test",
  agent: "claude_code",
  ref: "ref-1",
  peerId: "b".repeat(32),
  body: "Take over the flaky suite",
  handoff: {
    kind: "brief",
    id: "c".repeat(32),
    project: "acme",
    until: "2026-09-24T00:00:00Z",
  },
};

describe("PeerMessageView", () => {
  it("keeps the sender badge on a hand-off brief label", () => {
    render(<PeerMessageView message={handoffBrief} />);

    expect(
      screen.getByText("Hand-off · project acme · from Fix the flaky test · claude_code"),
    ).toBeInTheDocument();
  });
});
