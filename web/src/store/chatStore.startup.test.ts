import { afterEach, expect, it } from "vitest";
import { handleSessionEvent, useChatStore } from "./chatStore";
import { createInitialConversationState, isConversationStateKey } from "./conversationState";
import type { SessionStartupStatus } from "@/lib/types";

const startup: SessionStartupStatus = {
  id: "startup",
  attemptId: null,
  inputId: "input",
  state: "queued",
  reason: null,
  elapsedSeconds: 1,
  queuePositionEstimate: 1,
  canCancel: true,
  evidence: { runtimeReady: false, toolsReady: false, inputAccepted: false, workStarted: false },
};

afterEach(() => useChatStore.setState({ conversationId: null, startup: null }));

it("updates only the named session and resets startup on a fresh conversation", () => {
  useChatStore.setState({ conversationId: "visible", startup: null });
  handleSessionEvent({ type: "session_startup", conversationId: "other", startup }, "visible");
  expect(useChatStore.getState().startup).toBeNull();
  handleSessionEvent({ type: "session_startup", conversationId: "visible", startup }, "visible");
  expect(useChatStore.getState().startup).toEqual(startup);
  handleSessionEvent(
    { type: "session_startup", conversationId: "visible", startup: null },
    "visible",
  );
  expect(useChatStore.getState().startup).toBeNull();
  expect(createInitialConversationState().startup).toBeNull();
  expect(isConversationStateKey("startup")).toBe(true);
});
