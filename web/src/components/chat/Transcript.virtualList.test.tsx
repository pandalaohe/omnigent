import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import type { Bubble } from "@/lib/renderItems";
import { Conversation, ConversationContent } from "@/components/ai-elements/conversation";
import { CodeBlockSendContext } from "@/components/ai-elements/message";
import type { AnyBlock, BlockContext, ElicitationBlock } from "@/lib/blocks";
import { MemoryRouter } from "react-router-dom";
import { useChatStore } from "@/store/chatStore";
import type { Virtualizer } from "@tanstack/react-virtual";
import {
  isNativeFindShortcut,
  measureRowAndPin,
  Transcript,
  type TranscriptGeometry,
  VirtualBubbleList,
} from "./Transcript";

vi.mock("@/hooks/useArcaShutdownBanner", () => ({
  useArcaShutdownBanner: () => ({ showForHost: () => false }),
}));

// Module-scope so the provider value is stable (jsx-no-constructed-context-values).
const sendCodeBlock = vi.fn(() => true);

afterEach(() => {
  cleanup();
  useChatStore.setState({
    blocks: [],
    conversationId: null,
    sessionStatus: "idle",
    status: "idle",
    backgroundTaskCount: 0,
  });
});

const bubble: Extract<Bubble, { kind: "user" }> = {
  kind: "user",
  itemId: "user-1",
  content: [{ type: "input_text", text: "hello" }],
};

const assistantBubble: Extract<Bubble, { kind: "assistant" }> = {
  kind: "assistant",
  responseId: "response-1",
  stableId: "response-1",
  lifecycle: "completed",
  error: null,
  items: [{ kind: "text", itemId: "assistant-1", text: "hello back", final: true }],
};

function list(
  hasTasks: boolean,
  scrollEl: HTMLElement,
  onGeometryChange: (geometry: TranscriptGeometry) => void = vi.fn(),
  disableVirtualization = false,
) {
  return (
    <Conversation>
      <ConversationContent>
        <VirtualBubbleList
          bubbles={[bubble]}
          scrollEl={scrollEl}
          lastAssistantIndex={-1}
          showsWorking={false}
          sessionIdle
          conversationId={undefined}
          hasTasks={hasTasks}
          disableVirtualization={disableVirtualization}
          onGeometryChange={onGeometryChange}
        />
      </ConversationContent>
    </Conversation>
  );
}

function messageList(bubbles: Bubble[], showsWorking: boolean, sessionIdle: boolean) {
  return (
    <Conversation>
      <ConversationContent>
        <VirtualBubbleList
          bubbles={bubbles}
          scrollEl={null}
          lastAssistantIndex={bubbles.findLastIndex((item) => item.kind === "assistant")}
          showsWorking={showsWorking}
          sessionIdle={sessionIdle}
          conversationId="conv-1"
          hasTasks={false}
          disableVirtualization
          onGeometryChange={vi.fn()}
        />
      </ConversationContent>
    </Conversation>
  );
}

function actionFooter(button: HTMLElement): HTMLElement {
  return button.parentElement!.parentElement!;
}

it("remeasures scrollMargin when task padding changes without changing bubbles", async () => {
  const scrollEl = document.createElement("div");
  Object.defineProperties(scrollEl, {
    scrollTop: { configurable: true, writable: true, value: 0 },
    clientHeight: { configurable: true, value: 500 },
    scrollHeight: { configurable: true, value: 1_000 },
  });
  scrollEl.getBoundingClientRect = () => ({ top: 0 }) as DOMRect;

  const view = render(list(false, scrollEl));
  const row = view.container.querySelector<HTMLElement>('[data-index="0"]')!;
  expect(row).toHaveAttribute("data-bubble-key", "user:user-1");
  const wrapper = row.parentElement!;
  wrapper.getBoundingClientRect = () => ({ top: 64 }) as DOMRect;

  view.rerender(list(true, scrollEl));

  await waitFor(() => expect(row.style.transform).toBe("translateY(-64px)"));
});

it("pins after an observer re-measure of a row, never after a mount measure", () => {
  const pin = vi.fn();
  const measure = measureRowAndPin({ current: pin });
  const node = document.createElement("div");
  node.getBoundingClientRect = () => ({ height: 40 }) as DOMRect;
  const instance = { options: { horizontal: false } } as unknown as Virtualizer<
    HTMLElement,
    Element
  >;

  // Mounting measures without an entry; the row's height is the default measure.
  expect(measure(node, undefined, instance)).toBe(40);
  expect(pin).not.toHaveBeenCalled();

  // The virtualizer's ResizeObserver hands over an entry when a mounted row grew.
  const entry = {
    borderBoxSize: [{ blockSize: 64, inlineSize: 600 }],
  } as unknown as ResizeObserverEntry;
  expect(measure(node, entry, instance)).toBe(64);
  expect(pin).toHaveBeenCalledOnce();

  // Without a registered pin the measurement still goes through.
  expect(measureRowAndPin({ current: null })(node, entry, instance)).toBe(64);
  expect(measureRowAndPin(undefined)(node, entry, instance)).toBe(64);
});

it("publishes navigation that distinguishes loaded and missing turns", async () => {
  const scrollEl = document.createElement("div");
  const onGeometryChange = vi.fn<(geometry: TranscriptGeometry) => void>();

  render(list(false, scrollEl, onGeometryChange));

  await waitFor(() => expect(onGeometryChange).toHaveBeenCalled());
  const geometry = onGeometryChange.mock.calls.at(-1)![0];
  expect(geometry.scrollToItem("user-1")).toBe(true);
  expect(geometry.scrollToItem("missing")).toBe(false);
});

it("recognizes unhandled native find keyboard shortcuts", () => {
  expect(
    isNativeFindShortcut({
      key: "f",
      metaKey: true,
      ctrlKey: false,
      altKey: false,
      defaultPrevented: false,
    }),
  ).toBe(true);
  expect(
    isNativeFindShortcut({
      key: "f",
      metaKey: false,
      ctrlKey: true,
      altKey: false,
      defaultPrevented: false,
    }),
  ).toBe(true);
  expect(
    isNativeFindShortcut({
      key: "f",
      metaKey: true,
      ctrlKey: false,
      altKey: false,
      defaultPrevented: true,
    }),
  ).toBe(false);
});

it("renders every bubble in normal flow after native find is detected", () => {
  const scrollEl = document.createElement("div");

  const view = render(list(false, scrollEl, vi.fn(), true));

  expect(view.container.querySelector('[data-index="0"]')).toBeNull();
  expect(view.container.querySelectorAll('[data-testid="message-bubble"]')).toHaveLength(1);
});

it("keeps actions visible only on a final assistant message while idle", () => {
  const bubbles: Bubble[] = [bubble, assistantBubble, { kind: "compaction", itemId: "compact-1" }];
  const view = render(messageList(bubbles, false, true));
  const copyButtons = screen.getAllByRole("button", { name: "Copy" });

  expect(actionFooter(copyButtons[0]!)).toHaveClass("md:opacity-0");
  expect(actionFooter(copyButtons[1]!)).toHaveClass("opacity-100");
  expect(actionFooter(copyButtons[1]!)).not.toHaveClass(
    "md:opacity-0",
    "md:group-hover:opacity-100",
  );

  view.rerender(messageList(bubbles, false, false));
  expect(actionFooter(screen.getAllByRole("button", { name: "Copy" })[1]!)).toHaveClass(
    "md:opacity-0",
  );
});

it("keeps every action row hover-only when the final message is from the user", () => {
  render(messageList([assistantBubble, bubble], false, true));

  for (const button of screen.getAllByRole("button", { name: "Copy" })) {
    expect(actionFooter(button)).toHaveClass("md:opacity-0");
  }
});

const pendingElicitation: ElicitationBlock = {
  type: "elicitation",
  ctx: { agent: null, depth: 0, turn: 0, timestamp: 0, responseId: "resp_1", itemId: null },
  elicitationId: "elic_1",
  targetSessionId: null,
  message: "Allow shell command?",
  phase: "tool_call",
  policyName: "ask-before-shell",
  contentPreview: "{}",
  requestedSchema: {},
  url: null,
  status: "pending",
  response: null,
};

function renderTranscript(blocks: ElicitationBlock[]) {
  useChatStore.setState({ blocks, conversationId: "conv-test", sessionStatus: "running" });
  return render(
    <MemoryRouter>
      <Transcript
        hostId={null}
        setConversationEl={vi.fn()}
        containerEl={null}
        scroller={null}
        setScroller={vi.fn()}
        sendScrollNonce={0}
        hasMoreHistory={false}
        loadingMoreHistory={false}
        isMobileViewport
        showsWorking
        agentsError={null}
        sandboxLaunching={false}
        conversationId="conv-test"
        scrollToBottomOnSessionOpen={false}
        openedConversationIdRef={{ current: null }}
        spacerMeasureRef={{ current: null }}
      />
    </MemoryRouter>,
  );
}

function blockCtx(overrides: Partial<BlockContext> = {}): BlockContext {
  return {
    agent: "test",
    depth: 0,
    turn: 0,
    timestamp: 0,
    responseId: "resp_settled",
    itemId: null,
    ...overrides,
  };
}

// One settled turn: a tool run then a reply ending in a fenced block. Built
// from block shapes only so the walker derives lifecycle "completed".
const settledTurnBlocks: AnyBlock[] = [
  {
    type: "user_message",
    ctx: blockCtx({ itemId: "user-1" }),
    content: [{ type: "input_text", text: "do it" }],
  },
  {
    type: "response_start",
    ctx: blockCtx(),
    model: "test",
    responseId: "resp_settled",
    conversationId: "conv-test",
  },
  {
    type: "tool_group",
    ctx: blockCtx({ itemId: "tool-1" }),
    executions: [
      {
        name: "Bash",
        arguments: {},
        argsSummary: "",
        callId: "call_1",
        agentName: "test",
        executedBy: "server",
        output: null,
      },
    ],
    iteration: 0,
  },
  {
    type: "tool_result",
    ctx: blockCtx({ itemId: "result-1" }),
    name: "Bash",
    callId: "call_1",
    agentName: "test",
    output: "ok",
  },
  {
    type: "text_done",
    ctx: blockCtx({ itemId: "text-1" }),
    fullText: "Reply with:\n\n```\nok\n```\n",
    hasCodeBlocks: true,
  },
  {
    type: "response_end",
    ctx: blockCtx(),
    status: "completed",
    response: null,
  },
];

function renderSettledTurn(opts: {
  sessionStatus: "idle" | "running" | "waiting";
  status: "idle" | "streaming";
}) {
  useChatStore.setState({
    blocks: settledTurnBlocks,
    conversationId: "conv-test",
    sessionStatus: opts.sessionStatus,
    status: opts.status,
    backgroundTaskCount: 1,
  });
  const view = render(
    <MemoryRouter>
      <CodeBlockSendContext.Provider value={sendCodeBlock}>
        <Transcript
          hostId={null}
          setConversationEl={vi.fn()}
          containerEl={null}
          scroller={null}
          setScroller={vi.fn()}
          sendScrollNonce={0}
          hasMoreHistory={false}
          loadingMoreHistory={false}
          isMobileViewport
          showsWorking
          agentsError={null}
          sandboxLaunching={false}
          conversationId="conv-test"
          scrollToBottomOnSessionOpen={false}
          openedConversationIdRef={{ current: null }}
          spacerMeasureRef={{ current: null }}
        />
      </CodeBlockSendContext.Provider>
    </MemoryRouter>,
  );
  // Native find disables virtualization, so every bubble mounts for inspection.
  fireEvent.keyDown(window, { key: "f", metaKey: true });
  return view;
}

it("keeps a settled last reply settled while only background shells run", async () => {
  renderSettledTurn({ sessionStatus: "idle", status: "idle" });

  await screen.findByRole("button", { name: "Send as message" });
  expect(screen.getByRole("button", { name: /^Worked/ })).toBeInTheDocument();
});

it("keeps the live edge on the last reply while the turn is active", async () => {
  renderSettledTurn({ sessionStatus: "waiting", status: "idle" });

  await screen.findByRole("button", { name: "Toggle word wrap" });
  expect(screen.queryByRole("button", { name: "Send as message" })).toBeNull();
});

it("keeps the pending elicitation card after the working indicator", () => {
  renderTranscript([pendingElicitation]);

  const working = screen.getByTestId("working-indicator");
  const cards = screen.getAllByTestId("bottom-elicitation");
  const lastCard = cards.at(-1)!;

  expect(
    working.compareDocumentPosition(lastCard) & Node.DOCUMENT_POSITION_FOLLOWING,
    "Working indicator should precede the pending elicitation card",
  ).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
  expect(
    (lastCard.compareDocumentPosition(working) & Node.DOCUMENT_POSITION_FOLLOWING) !== 0,
    "No working indicator should follow the last pending elicitation card",
  ).toBe(false);
});

it("shows the working indicator when no elicitation is pending", () => {
  renderTranscript([]);

  expect(screen.getByTestId("working-indicator")).toBeInTheDocument();
  expect(screen.queryByTestId("bottom-elicitation")).not.toBeInTheDocument();
});

it("keeps a renamed top bubble's row across a history prepend", () => {
  // An assistant bubble is keyed by its first item, so a page that continues
  // the top turn renames it. The row must keep its node: a remount replays the
  // action row's hover fade on every page while older history loads.
  const scrollEl = document.createElement("div");
  Object.defineProperties(scrollEl, {
    scrollTop: { configurable: true, writable: true, value: 0 },
    clientHeight: { configurable: true, value: 500 },
    scrollHeight: { configurable: true, value: 1_000 },
  });
  scrollEl.getBoundingClientRect = () => ({ top: 0 }) as DOMRect;
  const transcript = (bubbles: Bubble[]) => (
    <Conversation>
      <ConversationContent>
        <VirtualBubbleList
          bubbles={bubbles}
          scrollEl={scrollEl}
          lastAssistantIndex={bubbles.length - 1}
          showsWorking={false}
          sessionIdle
          conversationId="conv-1"
          hasTasks={false}
          disableVirtualization={false}
          onGeometryChange={vi.fn()}
        />
      </ConversationContent>
    </Conversation>
  );

  const view = render(transcript([assistantBubble]));
  const row = view.container.querySelector<HTMLElement>('[data-index="0"]')!;
  expect(row).toHaveAttribute("data-bubble-key", "assistant:response-1");

  // The page lands an earlier item of the same response: same bubble, new key.
  const renamed: Bubble = {
    ...assistantBubble,
    stableId: "assistant-0",
    items: [
      { kind: "text", itemId: "assistant-0", text: "hello first", final: true },
      ...assistantBubble.items,
    ],
  };
  view.rerender(transcript([renamed]));

  expect(view.container.querySelector('[data-index="0"]')).toBe(row);
  expect(row).toHaveAttribute("data-bubble-key", "assistant:response-1");

  // A genuinely earlier turn is a new row above; the renamed bubble keeps its node.
  view.rerender(transcript([bubble, renamed]));
  expect(view.container.querySelector('[data-index="1"]')).toBe(row);
  expect(view.container.querySelector('[data-index="0"]')).toHaveAttribute(
    "data-bubble-key",
    "user:user-1",
  );
});
