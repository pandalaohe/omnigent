import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { setEmbedRoot } from "@/lib/host";
import {
  CodeBlockSendContext,
  Message,
  MessageAction,
  MessageActions,
  MessageContent,
  MessageResponse,
} from "./message";

const clipboardDescriptor = Object.getOwnPropertyDescriptor(Navigator.prototype, "clipboard");
const execCommandDescriptor = Object.getOwnPropertyDescriptor(Document.prototype, "execCommand");

afterEach(() => {
  setEmbedRoot(null);
  vi.unstubAllGlobals();
  cleanup();
  vi.restoreAllMocks();
  if (clipboardDescriptor) {
    Object.defineProperty(Navigator.prototype, "clipboard", clipboardDescriptor);
  } else {
    delete (Navigator.prototype as { clipboard?: unknown }).clipboard;
  }

  if (execCommandDescriptor) {
    Object.defineProperty(Document.prototype, "execCommand", execCommandDescriptor);
  } else {
    delete (Document.prototype as { execCommand?: unknown }).execCommand;
  }
});

describe("MessageContent", () => {
  it("uses the settings-driven interface text token", () => {
    render(<MessageContent>Message text</MessageContent>);

    const content = screen.getByText("Message text");
    expect(content).toHaveClass("text-ui", "group-[.is-user]:px-3", "group-[.is-user]:py-2");
    expect(content).not.toHaveClass("text-[0.8125rem]", "leading-[1.125rem]");
    expect(content).not.toHaveClass("group-[.is-user]:px-4", "group-[.is-user]:py-3");
  });
});

describe("Message", () => {
  it("keeps the message shrinkable", () => {
    render(<Message data-testid="message" from="assistant" />);

    expect(screen.getByTestId("message")).toHaveClass("min-w-0");
  });

  it("keeps a caller's width override alongside min-w-0", () => {
    render(<Message className="max-w-3xl" data-testid="message" from="assistant" />);

    const message = screen.getByTestId("message");
    expect(message).toHaveClass("min-w-0", "max-w-3xl");
  });
});

describe("MessageAction", () => {
  it("uses muted color by default and foreground color on hover", () => {
    render(
      <MessageAction label="Copy">
        <svg aria-hidden />
      </MessageAction>,
    );

    expect(screen.getByRole("button", { name: "Copy" })).toHaveClass(
      "text-muted-foreground",
      "hover:text-foreground",
    );
  });
});

describe("MessageActions", () => {
  it("uses 12px spacing between actions", () => {
    render(<MessageActions data-testid="message-actions">Actions</MessageActions>);

    expect(screen.getByTestId("message-actions")).toHaveClass("gap-3");
  });
});

// Streamdown renders a diagram only once an IntersectionObserver reports it
// visible; report every observed element visible so diagrams render in jsdom.
class VisibleIntersectionObserver {
  private readonly callback: IntersectionObserverCallback;
  constructor(callback: IntersectionObserverCallback) {
    this.callback = callback;
  }
  observe(target: Element) {
    this.callback(
      [{ isIntersecting: true, target } as IntersectionObserverEntry],
      this as unknown as IntersectionObserver,
    );
  }
  unobserve() {}
  disconnect() {}
  takeRecords(): IntersectionObserverEntry[] {
    return [];
  }
}

describe("MessageResponse", () => {
  it("blocks external image markdown and renders a placeholder", async () => {
    render(<MessageResponse>{"![leak](https://attacker.example/pixel.png)"}</MessageResponse>);

    expect(document.querySelector('img[src^="https://attacker.example"]')).toBeNull();
    expect(await screen.findByText("[Image blocked: leak]")).toBeTruthy();
  });

  it("re-renders when rendering props change even if the text is unchanged", async () => {
    const { container, rerender } = render(
      <MessageResponse className="math-config-a">same text</MessageResponse>,
    );

    expect(container.firstElementChild).toHaveClass("math-config-a");

    rerender(<MessageResponse className="math-config-b">same text</MessageResponse>);

    await waitFor(() => {
      expect(container.firstElementChild).toHaveClass("math-config-b");
    });
  });

  it("explains an invalid mermaid fence instead of dumping the parser error", async () => {
    vi.stubGlobal("IntersectionObserver", VisibleIntersectionObserver);
    render(
      <MessageResponse>
        {
          "```mermaid\nsequenceDiagram\n    A->>B: hi\n    Note over A,B: once; twice\n    A=>B: again\n```"
        }
      </MessageResponse>,
    );

    const card = await screen.findByTestId("mermaid-error", {}, { timeout: 10_000 });
    expect(card.textContent).toContain("Mermaid couldn't parse line 3");
    expect(card.querySelector("code")?.textContent).toBe("Note over A,B: once; twice");
    expect(card.textContent).toContain("#59;");
  }, 15_000);

  it("gives prose a break opportunity for an unbroken run (OMNI-2900)", () => {
    const { container } = render(<MessageResponse>same text</MessageResponse>);

    expect(container.firstElementChild).toHaveClass("wrap-anywhere");
  });

  it("keeps wrap-anywhere alongside a caller-supplied className", () => {
    const { container } = render(
      <MessageResponse className="math-config-a">same text</MessageResponse>,
    );

    expect(container.firstElementChild).toHaveClass("wrap-anywhere", "math-config-a");
  });
});

describe("MessageResponse table fullscreen", () => {
  const tableMarkdown = "| Name | Value |\n| --- | --- |\n| Alpha | One |";

  it("opens inside the embed root and closes from the fullscreen control", async () => {
    const embedRoot = document.createElement("div");
    document.body.appendChild(embedRoot);
    setEmbedRoot(embedRoot);
    render(<MessageResponse>{tableMarkdown}</MessageResponse>);

    fireEvent.click(await screen.findByRole("button", { name: "View fullscreen" }));

    const dialog = within(embedRoot).getByRole("dialog", { name: "View fullscreen" });
    expect(within(dialog).getByRole("cell", { name: "Alpha" })).toBeInTheDocument();

    fireEvent.click(within(dialog).getByRole("button", { name: "Exit fullscreen" }));
    expect(within(embedRoot).queryByRole("dialog", { name: "View fullscreen" })).toBeNull();
    embedRoot.remove();
  });

  it("closes the fullscreen table with Escape", async () => {
    render(<MessageResponse>{tableMarkdown}</MessageResponse>);
    fireEvent.click(await screen.findByRole("button", { name: "View fullscreen" }));

    expect(screen.getByRole("dialog", { name: "View fullscreen" })).toBeInTheDocument();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("dialog", { name: "View fullscreen" })).toBeNull();
  });
});

describe("MessageResponse code-block copy", () => {
  it("wraps code by default and exposes the wrap state through the toggle", async () => {
    const { container } = render(
      <MessageResponse>{"```ts\nconst value = 'horizontalScrolling';\n```"}</MessageResponse>,
    );

    const toggle = await screen.findByRole("button", { name: "Toggle word wrap" });
    const block = container.querySelector(".chat-code-block");
    expect(toggle).toHaveAttribute("aria-pressed", "true");
    expect(block).toHaveClass("chat-code-wrap");

    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-pressed", "false");
    expect(block).not.toHaveClass("chat-code-wrap");

    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-pressed", "true");
    expect(block).toHaveClass("chat-code-wrap");
  });

  it("copies the exact fenced code text through the fallback path", async () => {
    const copiedText: string[] = [];
    Object.defineProperty(Navigator.prototype, "clipboard", {
      configurable: true,
      value: undefined,
    });
    Object.defineProperty(Document.prototype, "execCommand", {
      configurable: true,
      value: vi.fn((command: string) => {
        expect(command).toBe("copy");
        const event = new Event("copy", {
          bubbles: true,
          cancelable: true,
        }) as ClipboardEvent;
        Object.defineProperty(event, "clipboardData", {
          configurable: true,
          value: {
            setData: (type: string, value: string) => {
              expect(type).toBe("text/plain");
              copiedText.push(value);
            },
          },
        });
        document.dispatchEvent(event);
        return true;
      }),
    });

    render(
      <MessageResponse>{"```ts\nconst value = 1;\nconsole.log(value);\n```"}</MessageResponse>,
    );

    fireEvent.click(await screen.findByRole("button", { name: "Copy Code" }));

    await waitFor(() => {
      expect(copiedText).toEqual(["const value = 1;\nconsole.log(value);\n"]);
    });
    expect(screen.getByRole("button", { name: "Download file" })).toBeInTheDocument();
  });
});

describe("MessageResponse code-block send", () => {
  it("sends the whole trimmed block through a button ahead of wrap and copy", async () => {
    const send = vi.fn(() => true);
    render(
      <CodeBlockSendContext.Provider value={send}>
        <MessageResponse sendCodeBlocks>{"```ts\nfirst line\nsecond line\n```"}</MessageResponse>
      </CodeBlockSendContext.Provider>,
    );

    const sendButton = await screen.findByRole("button", { name: "Send as message" });
    const wrapButton = screen.getByRole("button", { name: "Toggle word wrap" });
    const copyButton = screen.getByRole("button", { name: "Copy Code" });
    const follows = Node.DOCUMENT_POSITION_FOLLOWING;

    expect(sendButton.parentElement).toBe(wrapButton.parentElement);
    expect(sendButton.compareDocumentPosition(wrapButton) & follows).toBe(follows);
    expect(wrapButton.compareDocumentPosition(copyButton) & follows).toBe(follows);

    fireEvent.click(sendButton);
    expect(send).toHaveBeenCalledTimes(1);
    expect(send).toHaveBeenCalledWith("first line\nsecond line");
  });

  it("renders no send button without the opt-in or without a provider", async () => {
    const { unmount } = render(
      <CodeBlockSendContext.Provider value={vi.fn(() => true)}>
        <MessageResponse>{"```ts\nconst value = 1;\n```"}</MessageResponse>
      </CodeBlockSendContext.Provider>,
    );
    await screen.findByRole("button", { name: "Toggle word wrap" });
    expect(screen.queryByRole("button", { name: "Send as message" })).toBeNull();
    unmount();

    render(<MessageResponse sendCodeBlocks>{"```ts\nconst value = 1;\n```"}</MessageResponse>);
    await screen.findByRole("button", { name: "Toggle word wrap" });
    expect(screen.queryByRole("button", { name: "Send as message" })).toBeNull();
  });

  it("shows the check state after a successful send and ignores clicks while it shows", async () => {
    const send = vi.fn(() => true);
    const { unmount } = render(
      <CodeBlockSendContext.Provider value={send}>
        <MessageResponse sendCodeBlocks>{"```ts\nconst value = 1;\n```"}</MessageResponse>
      </CodeBlockSendContext.Provider>,
    );
    const button = await screen.findByRole("button", { name: "Send as message" });

    fireEvent.click(button);

    expect(button.querySelector("path")).toHaveAttribute(
      "d",
      "M15.5607 3.99999L15.0303 4.53032L6.23744 13.3232C5.55403 14.0066 4.44599 14.0066 3.76257 13.3232L4.2929 12.7929L3.76257 13.3232L0.969676 10.5303L0.439346 9.99999L1.50001 8.93933L2.03034 9.46966L4.82323 12.2626C4.92086 12.3602 5.07915 12.3602 5.17678 12.2626L13.9697 3.46966L14.5 2.93933L15.5607 3.99999Z",
    );
    fireEvent.click(button);
    expect(send).toHaveBeenCalledTimes(1);
    unmount();

    const refuse = vi.fn(() => false);
    render(
      <CodeBlockSendContext.Provider value={refuse}>
        <MessageResponse sendCodeBlocks>{"```ts\nconst value = 1;\n```"}</MessageResponse>
      </CodeBlockSendContext.Provider>,
    );
    const refusedButton = await screen.findByRole("button", { name: "Send as message" });

    fireEvent.click(refusedButton);
    fireEvent.click(refusedButton);

    expect(refuse).toHaveBeenCalledTimes(2);
    expect(refuse).toHaveBeenLastCalledWith("const value = 1;");
  });
});
