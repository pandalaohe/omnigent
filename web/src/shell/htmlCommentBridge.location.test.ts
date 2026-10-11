import { expect, it } from "vitest";
// @ts-expect-error jsdom ships no declarations; exercised through the real runtime.
import { JSDOM } from "jsdom";
import bridgeSource from "../../public/omni-html-bridge.js?raw";

it("captures the selected overlapping quote and a unique node address", () => {
  const dom = new JSDOM('<main><p id="duplicate">aaaa</p><p id="duplicate">aaaa</p></main>', {
    url: "https://example.test/preview",
    runScripts: "outside-only",
  });
  const win = dom.window;
  const messages: Record<string, unknown>[] = [];
  const script = win.document.createElement("script");
  script.dataset.omniNonce = "location";
  Object.defineProperty(win.document, "currentScript", { value: script, configurable: true });
  Object.defineProperty(win, "CSS", { value: { escape: (s: string) => s } });
  Object.defineProperty(win.Range.prototype, "getClientRects", {
    value: () => [{ left: 0, top: 0, right: 10, bottom: 10, width: 10, height: 10 }],
  });
  win.eval(bridgeSource);
  win.dispatchEvent(
    new win.MessageEvent("message", {
      data: { type: "omni:init", source: "omni-html-comment", nonce: "location" },
      ports: [
        { postMessage: (message: Record<string, unknown>) => messages.push(message) },
      ] as unknown as MessagePort[],
    }),
  );
  try {
    const target = win.document.querySelectorAll("p")[1];
    const range = win.document.createRange();
    range.setStart(target.firstChild!, 1);
    range.setEnd(target.firstChild!, 3);
    win.getSelection()!.addRange(range);
    win.document.dispatchEvent(new win.MouseEvent("mouseup", { bubbles: true }));
    const selected = messages.find((m) => m.type === "omni:selection")!;
    expect(selected.text).toBe("aa");
    expect(selected.prefix).toBe("aaaa a");
    expect(selected.suffix).toBe("a");
    expect(win.document.querySelectorAll(selected.selector as string)).toHaveLength(1);
    expect(win.document.querySelector(selected.selector as string)).toBe(target);
  } finally {
    win.close();
  }
});
