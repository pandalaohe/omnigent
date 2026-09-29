import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import type { ParsedPeerMessage } from "@/lib/peerMessage";
import { PeerMessageView } from "./PeerMessage";

afterEach(cleanup);

const peerMessage: ParsedPeerMessage = {
  senderId: "a".repeat(32),
  title: "Fix the flaky test",
  agent: "claude_code",
  ref: "ref-1",
  peerId: "b".repeat(32),
  body: "Take over the flaky suite",
};

describe("PeerMessageView", () => {
  it("renders the sender badge on the plain peer label", () => {
    render(<PeerMessageView message={peerMessage} />);

    expect(screen.getByText("From session Fix the flaky test · claude_code")).toBeInTheDocument();
  });
});
