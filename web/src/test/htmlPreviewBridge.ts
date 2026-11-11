/** Connect a test to a mounted HTML preview's comment bridge. */
import { fireEvent, screen, waitFor } from "@testing-library/react";
import { expect, vi } from "vitest";
import { BRIDGE_SOURCE } from "@/shell/htmlCommentBridge";

/**
 * Wait for the viewer's bridge handshake and return the frame's end of the port.
 *
 * @param iframe The mounted preview frame.
 * @returns The port plus `send`, which posts a message carrying the bridge's source and nonce.
 */
export async function connectHtmlPreviewBridge(iframe: HTMLIFrameElement) {
  if (!iframe.contentWindow) throw new Error("Preview has no content window");
  const postMessage = vi.spyOn(iframe.contentWindow, "postMessage");
  fireEvent.load(iframe);
  const commentInits = () =>
    postMessage.mock.calls.filter(
      ([message]) => (message as { type?: string }).type === "omni:init",
    );
  await waitFor(() => expect(commentInits().length).toBeGreaterThan(0));
  const [init, , transfer] = commentInits().at(-1) as unknown as [
    { nonce: string; restoreScroll?: { top: number; budgetMs: number } | null },
    string,
    MessagePort[],
  ];
  const port = transfer[0];
  const send = (msg: Record<string, unknown>) =>
    port.postMessage({ source: BRIDGE_SOURCE, nonce: init.nonce, ...msg });
  return { port, send, init };
}

/** The saved offset the mounted preview was asked to restore, or null for none. */
export function requestedScrollRestore(): { top: number } | null {
  const srcDoc = screen.getByTitle<HTMLIFrameElement>("HTML preview").getAttribute("srcdoc");
  const doc = new DOMParser().parseFromString(srcDoc ?? "", "text/html");
  const value = doc
    .querySelector("script[data-omni-restore-scroll]")
    ?.getAttribute("data-omni-restore-scroll");
  return value ? (JSON.parse(value) as { top: number }) : null;
}
