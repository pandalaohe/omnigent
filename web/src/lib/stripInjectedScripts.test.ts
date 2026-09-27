import { describe, expect, it } from "vitest";
import { stripInjectedScripts } from "./stripInjectedScripts";

describe("stripInjectedScripts", () => {
  const nonce = "abc123";

  it("removes every injected bridge script and returns the exact original bytes", () => {
    const original = "<html><head><title>t</title></head><body><p>hi</p></body></html>";
    const injected = original.replace(
      "</body>",
      `<script data-omni-nonce="${nonce}">var close = "<\\/script>";</script>` +
        `<script data-omni-nonce="${nonce}">bridge();</script></body>`,
    );

    // The first tag embeds an escaped `<\/script>` and must not terminate early.
    expect(stripInjectedScripts(injected, nonce)).toBe(original);
  });

  it("leaves scripts that are not the server's injected bridge alone", () => {
    const html = `<body><script src="app.js"></script><script data-omni-nonce="other">x</script></body>`;
    expect(stripInjectedScripts(html, nonce)).toBe(html);
  });
});
