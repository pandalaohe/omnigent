import { act, cleanup, fireEvent, render } from "@testing-library/react";
import { ScrollToBottomOnSend } from "@/components/chat/chatBubbleParts";
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
