import { createRef } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { setEmbedRoot } from "@/lib/host";
import { SelectionPopup } from "./SelectionPopup";

afterEach(() => {
  cleanup();
  setEmbedRoot(null);
  window.getSelection()?.removeAllRanges();
});

it.each([true, false])("portals outside the transcript (embedded: %s)", (embedded) => {
  const containerRef = createRef<HTMLDivElement>();
  const onReply = vi.fn();
  const { container } = render(
    <QueryClientProvider client={new QueryClient()}>
      <div ref={containerRef}>Selected message</div>
      <SelectionPopup containerRef={containerRef} onReply={onReply} />
    </QueryClientProvider>,
  );
  if (embedded) setEmbedRoot(container);

  const range = document.createRange();
  range.selectNodeContents(containerRef.current!);
  range.getBoundingClientRect = () => new DOMRect(10, 30, 80, 20);
  window.getSelection()!.addRange(range);
  fireEvent(document, new Event("selectionchange"));

  const reply = screen.getByRole("button", { name: "Reply ↵" });
  const popup = reply.closest("[style]");
  expect(popup?.parentElement).toBe(embedded ? container : document.body);
  expect(popup).toHaveStyle({ position: "fixed", left: "50px", top: "30px" });
  fireEvent.click(reply);
  expect(onReply).toHaveBeenCalledExactlyOnceWith("Selected message");
  expect(screen.queryByRole("button", { name: "Reply ↵" })).toBeNull();
});
