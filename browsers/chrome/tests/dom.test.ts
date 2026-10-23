// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { elementTarget, metrics, nodeAddress, selectedText, visibleText } from "../src/picker/dom";

afterEach(() => {
  document.body.replaceChildren();
  getSelection()?.removeAllRanges();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});
describe("isolated picker DOM evidence", () => {
  it.each(["CSS1Compat", "BackCompat"])(
    "measures scrollbar-excluding viewport client dimensions in %s",
    (compatMode) => {
      vi.spyOn(document, "compatMode", "get").mockReturnValue(compatMode);
      vi.spyOn(document.documentElement, "clientWidth", "get").mockReturnValue(985);
      vi.spyOn(document.documentElement, "clientHeight", "get").mockReturnValue(800);
      vi.spyOn(document.body, "clientWidth", "get").mockReturnValue(1000);
      vi.spyOn(document.body, "clientHeight", "get").mockReturnValue(785);
      vi.stubGlobal("innerWidth", 1000);
      vi.stubGlobal("innerHeight", 800);
      vi.stubGlobal("visualViewport", undefined);
      expect(metrics(2)).toMatchObject({
        width: 1000,
        height: 800,
        clientWidth: compatMode === "BackCompat" ? 1000 : 985,
        clientHeight: compatMode === "BackCompat" ? 785 : 800,
        visual: {
          width: compatMode === "BackCompat" ? 1000 : 985,
          height: compatMode === "BackCompat" ? 785 : 800,
        },
      });
    },
  );
  it("keeps the closest 32 characters before and after an element", () => {
    const before = document.createElement("p");
    before.textContent = "z".repeat(300) + " nearest before";
    const target = document.createElement("button");
    target.textContent = "Save";
    const after = document.createElement("p");
    after.textContent = "nearest after " + "z".repeat(300);
    document.body.append(before, target, after);
    expect(elementTarget(target).prefix).toBe("z".repeat(17) + " nearest before");
    expect(elementTarget(target).suffix).toBe("nearest after " + "z".repeat(18));
  });
  it("uses a unique positional selector rather than hostile IDs/classes", () => {
    const first = document.createElement("button");
    const second = document.createElement("button");
    second.id = 'a"\\bad';
    document.body.append(first, second);
    expect(nodeAddress(second)).toBe(
      "html:nth-of-type(1) > body:nth-of-type(1) > button:nth-of-type(2)",
    );
  });
  it("omits form values, hidden content and script text", () => {
    const div = document.createElement("div");
    const text = document.createElement("span");
    text.textContent = "Visible";
    const hidden = document.createElement("span");
    hidden.hidden = true;
    hidden.textContent = "PRIVATE";
    const textarea = document.createElement("textarea");
    textarea.value = "PRIVATE";
    textarea.textContent = "PRIVATE";
    const script = document.createElement("script");
    script.type = "application/json";
    script.textContent = "PRIVATE";
    div.append(text, hidden, textarea, script);
    document.body.append(div);
    expect(visibleText(div)).toBe("Visible");
    expect(elementTarget(text).quote).toBe("Visible");
  });
  it("leaves normal CSS selector unavailable across a shadow boundary and for embedded content", () => {
    const host = document.createElement("div");
    const shadow = host.attachShadow({ mode: "open" });
    const span = document.createElement("span");
    span.textContent = "Shadow";
    shadow.append(span);
    document.body.append(host);
    expect(elementTarget(span).selector).toBe("");
    expect(elementTarget(span).quote).toBe("Shadow");
    const iframe = document.createElement("iframe");
    document.body.append(iframe);
    expect(elementTarget(iframe)).toMatchObject({ selector: "", quote: "" });
  });
  it("freezes the actual selected quote and bounded adjacent context", () => {
    const p = document.createElement("p");
    p.textContent = "Before selected after";
    document.body.append(p);
    const range = document.createRange();
    range.setStart(p.firstChild!, 7);
    range.setEnd(p.firstChild!, 15);
    getSelection()?.addRange(range);
    expect(selectedText().target).toMatchObject({
      quote: "selected",
      prefix: "Before",
      suffix: "after",
    });
  });
  it("rejects selections in editable controls", () => {
    const p = document.createElement("p");
    p.setAttribute("contenteditable", "true");
    p.textContent = "Private";
    document.body.append(p);
    const range = document.createRange();
    range.selectNodeContents(p);
    getSelection()?.addRange(range);
    expect(() => selectedText()).toThrow("form controls");
  });
  it("omits CSS-hidden ancestors from both element quote and selected-text context", () => {
    const p = document.createElement("p");
    const before = document.createTextNode("Before ");
    const hidden = document.createElement("span");
    hidden.style.display = "none";
    const nested = document.createElement("span");
    nested.textContent = "PRIVATE";
    hidden.append(nested);
    const quote = document.createTextNode("selected");
    const after = document.createTextNode(" after");
    p.append(before, hidden, quote, after);
    document.body.append(p);
    expect(visibleText(p)).toBe("Before selected after");
    const range = document.createRange();
    range.setStart(quote, 0);
    range.setEnd(quote, 8);
    getSelection()?.addRange(range);
    expect(selectedText().target).toMatchObject({
      quote: "selected",
      prefix: "Before",
      suffix: "after",
    });
  });
});
