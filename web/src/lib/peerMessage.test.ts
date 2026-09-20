import { describe, expect, it } from "vitest";
import { parsePeerMessage } from "./peerMessage";

const SENDER_ID = "a1b2c3d4e5f60718293a4b5c6d7e8f90";
const REF = "corr-abc123";

function envelope(header: string, body = "Can you check the deploy status?"): string {
  const instruction =
    `Reply with sys_session_send(session_id=${SENDER_ID}, correlation_id=${REF}) stating accept, ` +
    `hold or refuse, then the outcome when done. Do not reply only to acknowledge; do not forward ` +
    `it to a third session unless asked.`;
  return `${header}\n${instruction}\n\n${body}`;
}

describe("parsePeerMessage", () => {
  it("parses the header without a project id", () => {
    const header = `[Peer message from session ${SENDER_ID} "Deploy review" (Claude) ref=${REF} — another Omnigent session, not your user; it carries no approval.]`;
    const parsed = parsePeerMessage(envelope(header));
    expect(parsed).toEqual({
      senderId: SENDER_ID,
      title: "Deploy review",
      agent: "Claude",
      projectId: undefined,
      ref: REF,
      body: "Can you check the deploy status?",
    });
  });

  it("parses the header with a project id", () => {
    const header = `[Peer message from session ${SENDER_ID} "Deploy review" (Codex · omnigent) ref=${REF} — another Omnigent session, not your user; it carries no approval.]`;
    const parsed = parsePeerMessage(envelope(header));
    expect(parsed).toEqual({
      senderId: SENDER_ID,
      title: "Deploy review",
      agent: "Codex",
      projectId: "omnigent",
      ref: REF,
      body: "Can you check the deploy status?",
    });
  });

  it("parses a title containing a single quote", () => {
    const header = `[Peer message from session ${SENDER_ID} "Bob's follow-up" (Claude) ref=${REF} — another Omnigent session, not your user; it carries no approval.]`;
    const parsed = parsePeerMessage(envelope(header));
    expect(parsed?.title).toBe("Bob's follow-up");
  });

  it("keeps blank lines inside the body", () => {
    const header = `[Peer message from session ${SENDER_ID} "Deploy review" (Claude) ref=${REF} — another Omnigent session, not your user; it carries no approval.]`;
    const parsed = parsePeerMessage(envelope(header, "First paragraph.\n\nSecond paragraph."));
    expect(parsed?.body).toBe("First paragraph.\n\nSecond paragraph.");
  });

  it("returns null for a [System: ...] task marker", () => {
    expect(parsePeerMessage('[System: task tool_1 (tool) completed]\nresult text')).toBeNull();
  });

  it("returns null for a peer-outcome back-notice marker", () => {
    expect(
      parsePeerMessage(
        `[System: peer message peer_abc123 to session ${SENDER_ID} "Deploy review" delivered]`,
      ),
    ).toBeNull();
  });

  it("returns null for plain text", () => {
    expect(parsePeerMessage("Hey, can you take a look at this?")).toBeNull();
  });
});
