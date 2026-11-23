import { act, cleanup, fireEvent, render } from "@testing-library/react";
import { ScrollToBottomOnSend } from "@/components/chat/chatBubbleParts";
import { useQuestionCardScroll } from "@/hooks/useQuestionCardScroll";
import { useRef, useState } from "react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { Conversation, ConversationContent, ConversationScrollButton } from "./conversation";

let resizeContent: Array<(height: number) => void>;

beforeEach(() => {
  vi.useFakeTimers();
  resizeContent = [];
  vi.stubGlobal(
    "PointerEvent",
    class extends MouseEvent {
      pointerType: string;
      constructor(type: string, init: PointerEventInit = {}) {
        super(type, init);
        this.pointerType = init.pointerType ?? "";
      }
    },
  );
  vi.stubGlobal(
    "ResizeObserver",
    class {
      constructor(callback: ResizeObserverCallback) {
        resizeContent.push((height) =>
          callback([{ contentRect: { height } } as ResizeObserverEntry], this as ResizeObserver),
        );
      }
      observe() {}
      disconnect() {}
      unobserve() {}
    },
  );
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

it("holds a question card through stream growth and resumes the original bottom follow", async () => {
  function Card({ held }: { held: boolean }) {
    const ref = useRef<HTMLDivElement>(null);
    useQuestionCardScroll(ref, held, null);
    return (
      <div
        ref={ref}
        data-testid="question"
        onKeyDown={(event) => {
          if (event.key === "ArrowDown") event.preventDefault();
        }}
      >
        <button type="button">Question option</button>
      </div>
    );
  }
  const transcript = (held: boolean) => (
    <div className="session-chat-transcript">
      <Conversation>
        <ConversationContent>
          <Card held={held} />
        </ConversationContent>
      </Conversation>
    </div>
  );
  const view = render(transcript(false));
  const scroller = view.getByRole("log").firstElementChild as HTMLElement;
  const card = view.getByTestId("question");
  let height = 1000;
  let cardOffset = 680;
  Object.defineProperties(scroller, {
    clientHeight: { get: () => 400 },
    scrollHeight: { get: () => height },
  });
  vi.spyOn(card, "getBoundingClientRect").mockImplementation(
    () => new DOMRect(0, cardOffset - scroller.scrollTop, 200, 100),
  );
  const settle = () =>
    act(async () => {
      resizeContent.forEach((resize) => resize(height));
      await vi.advanceTimersByTimeAsync(100);
    });
  await settle();
  expect(scroller.scrollTop).toBe(599);

  view.rerender(transcript(true));
  fireEvent.keyDown(card, { key: "ArrowDown" });
  fireEvent.keyDown(view.getByRole("button", { name: "Question option" }), { key: " " });
  expect(scroller).toHaveAttribute("data-question-card-scroll-held");
  const initialTop = card.getBoundingClientRect().top;
  // When content above the card shrinks at the bottom, the browser has
  // already clamped scrollTop by nearly the whole layout change.
  cardOffset -= 80;
  height -= 80;
  scroller.scrollTop = height - scroller.clientHeight;
  fireEvent.scroll(scroller);
  await settle();
  expect(Math.abs(card.getBoundingClientRect().top - initialTop)).toBeLessThanOrEqual(1);
  const clampedTop = scroller.scrollTop;

  cardOffset += 200;
  height += 300; // 200px of output above, 100px of content below.
  await settle();
  expect(Math.abs(card.getBoundingClientRect().top - initialTop)).toBeLessThanOrEqual(1);
  expect(scroller.scrollTop).toBe(clampedTop + 200);

  view.rerender(transcript(false));
  await settle();
  expect(scroller.scrollTop).toBe(819);
  height += 100;
  await settle();
  expect(scroller.scrollTop).toBe(919);
});

it.each(["touch", "wheel", "keyboard"])(
  "keeps %s scrolling released through a resize until the actual bottom",
  async (input) => {
    const view = render(
      <Conversation>
        <ConversationContent>Streaming transcript</ConversationContent>
      </Conversation>,
    );
    const scroller = view.getByRole("log").firstElementChild as HTMLElement;
    let height = 1000;
    Object.defineProperties(scroller, {
      clientHeight: { get: () => 400 },
      scrollHeight: { get: () => height },
    });
    await act(async () => {
      resizeContent.forEach((resize) => resize(height));
      await vi.advanceTimersByTimeAsync(100);
    });
    expect(scroller.scrollTop).toBe(599);
    if (input === "touch") {
      fireEvent.touchStart(scroller, { touches: [{ identifier: 1, clientX: 100, clientY: 100 }] });
      fireEvent.touchMove(scroller, { touches: [{ identifier: 1, clientX: 100, clientY: 160 }] });
    } else if (input === "wheel") fireEvent.wheel(scroller, { deltaY: -60 });
    else fireEvent.keyDown(scroller, { key: "PageUp" });
    await act(async () => {
      scroller.scrollTop = 539;
      fireEvent.scroll(scroller);
      height = 1100;
      resizeContent.forEach((resize) => resize(height));
      await vi.advanceTimersByTimeAsync(100);
    });
    expect(scroller.scrollTop).toBe(539);
    await act(async () => {
      height = 980;
      resizeContent.forEach((resize) => resize(height));
      await vi.advanceTimersByTimeAsync(100);
      height = 1100;
      resizeContent.forEach((resize) => resize(height));
      await vi.advanceTimersByTimeAsync(100);
    });
    expect(scroller.scrollTop).toBe(539);
    await act(async () => {
      scroller.scrollTop = 679;
      fireEvent.scroll(scroller);
      await vi.advanceTimersByTimeAsync(100);
      height = 1200;
      resizeContent.forEach((resize) => resize(height));
      await vi.advanceTimersByTimeAsync(100);
    });
    expect(scroller.scrollTop).toBe(679);
    await act(async () => {
      scroller.scrollTop = 799;
      fireEvent.scroll(scroller);
      await vi.advanceTimersByTimeAsync(100);
      height = 1300;
      resizeContent.forEach((resize) => resize(height));
      await vi.advanceTimersByTimeAsync(100);
    });
    expect(scroller.scrollTop).toBe(899);
  },
);

it("leaves scrolling inside a nested control independent of the transcript lock", async () => {
  const view = render(
    <Conversation>
      {() => (
        <ConversationContent>
          <textarea
            data-testid="nested"
            style={{ overflow: "auto", overflowY: "auto" }}
            defaultValue="Scrollable code"
          />
        </ConversationContent>
      )}
    </Conversation>,
  );
  const scroller = view.getByRole("log").firstElementChild as HTMLElement;
  const nested = view.getByTestId("nested");
  let height = 1000;
  Object.defineProperties(scroller, {
    clientHeight: { get: () => 400 },
    scrollHeight: { get: () => height },
  });
  Object.defineProperties(nested, {
    clientHeight: { get: () => 100 },
    scrollHeight: { get: () => 600 },
  });
  await act(async () => {
    resizeContent.forEach((resize) => resize(height));
    await vi.advanceTimersByTimeAsync(100);
  });
  fireEvent.pointerDown(nested, { pointerType: "touch" });
  fireEvent.touchStart(nested, { touches: [{ clientX: 100, clientY: 100 }] });
  fireEvent.pointerCancel(nested, { pointerType: "touch" });
  fireEvent.touchMove(nested, { touches: [{ clientX: 100, clientY: 160 }] });
  fireEvent.wheel(nested, { deltaY: -60 });
  fireEvent.keyDown(nested, { key: "PageUp" });
  await act(async () => {
    height = 1100;
    resizeContent.forEach((resize) => resize(height));
    await vi.advanceTimersByTimeAsync(100);
  });
  expect(scroller.scrollTop).toBe(699);
});

it.each(["click", "arrow key"])(
  "re-anchors a released question card on a caret %s in the still-focused answer",
  async (action) => {
    function Card() {
      const ref = useRef<HTMLDivElement>(null);
      const [field, setField] = useState<HTMLTextAreaElement | null>(null);
      useQuestionCardScroll(ref, field !== null, field);
      return (
        <div ref={ref}>
          <textarea
            aria-label="Answer"
            defaultValue="draft"
            onFocus={(event) => setField(event.currentTarget)}
          />
        </div>
      );
    }
    const view = render(
      <div className="session-chat-transcript">
        <Conversation>
          <ConversationContent>
            <Card />
          </ConversationContent>
        </Conversation>
      </div>,
    );
    const scroller = view.getByRole("log").firstElementChild as HTMLElement;
    const answer = view.getByRole("textbox", { name: "Answer" });
    act(() => answer.focus());
    expect(scroller).toHaveAttribute("data-question-card-scroll-held");
    fireEvent.wheel(scroller, { deltaY: -60 });
    expect(answer).toHaveFocus();
    expect(scroller).not.toHaveAttribute("data-question-card-scroll-held");

    fireEvent(document, new Event("selectionchange"));
    expect(scroller).not.toHaveAttribute("data-question-card-scroll-held");
    if (action === "arrow key") {
      window.addEventListener("keydown", (event) => event.preventDefault(), { once: true });
      fireEvent.keyDown(answer, { key: "ArrowDown", altKey: true, metaKey: true });
      await act(() => vi.advanceTimersByTimeAsync(32));
      expect(scroller).not.toHaveAttribute("data-question-card-scroll-held");
    }
    const positionCaret = () => {
      if (action === "click") fireEvent.click(answer);
      else fireEvent.keyDown(answer, { key: "ArrowLeft" });
    };
    positionCaret();
    fireEvent.wheel(scroller, { deltaY: 60 });
    await act(() => vi.advanceTimersByTimeAsync(32));
    expect(scroller).not.toHaveAttribute("data-question-card-scroll-held");

    positionCaret();
    await act(() => vi.advanceTimersByTimeAsync(32));
    expect(answer).toHaveFocus();
    expect(scroller).toHaveAttribute("data-question-card-scroll-held");
  },
);

it.each(["button", "send"])(
  "honors an explicit latest %s even when content resizes before its scroll frame",
  async (action) => {
    const transcript = (nonce: number) => (
      <Conversation>
        <ConversationContent>Transcript</ConversationContent>
        <ScrollToBottomOnSend nonce={nonce} />
        <ConversationScrollButton />
      </Conversation>
    );
    const view = render(transcript(0));
    const scroller = view.getByRole("log").firstElementChild as HTMLElement;
    let height = 1000;
    Object.defineProperties(scroller, {
      clientHeight: { get: () => 400 },
      scrollHeight: { get: () => height },
    });
    await act(async () => {
      resizeContent.forEach((resize) => resize(height));
      await vi.advanceTimersByTimeAsync(100);
    });
    fireEvent.wheel(scroller, { deltaY: -200 });
    await act(async () => {
      scroller.scrollTop = 400;
      fireEvent.scroll(scroller);
      await vi.advanceTimersByTimeAsync(100);
    });
    act(() => {
      if (action === "button") fireEvent.click(view.getByRole("button"));
      else view.rerender(transcript(1));
    });
    await act(async () => {
      height = 1200;
      resizeContent.forEach((resize) => resize(height));
      await vi.advanceTimersByTimeAsync(100);
    });
    expect(scroller.scrollTop).toBe(799);
  },
);

it("honors Send when a near-bottom negative resize precedes the scroll frame", async () => {
  const transcript = (nonce: number) => (
    <Conversation>
      <ConversationContent>Transcript</ConversationContent>
      <ScrollToBottomOnSend nonce={nonce} />
    </Conversation>
  );
  const view = render(transcript(0));
  const scroller = view.getByRole("log").firstElementChild as HTMLElement;
  let height = 1000;
  Object.defineProperties(scroller, {
    clientHeight: { get: () => 400 },
    scrollHeight: { get: () => height },
  });
  await act(async () => {
    resizeContent.forEach((resize) => resize(height));
    await vi.advanceTimersByTimeAsync(100);
  });
  fireEvent.wheel(scroller, { deltaY: -60 });
  await act(async () => {
    scroller.scrollTop = 539;
    fireEvent.scroll(scroller);
    await vi.advanceTimersByTimeAsync(100);
  });
  view.rerender(transcript(1));
  await act(async () => {
    height = 980;
    resizeContent.forEach((resize) => resize(height));
    await vi.advanceTimersByTimeAsync(100);
  });
  expect(scroller.scrollTop).toBe(579);
  await act(async () => {
    height = 1100;
    resizeContent.forEach((resize) => resize(height));
    await vi.advanceTimersByTimeAsync(100);
  });
  expect(scroller.scrollTop).toBe(699);
});
