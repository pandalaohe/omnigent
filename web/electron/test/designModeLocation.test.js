const assert = require("node:assert/strict");
const { it } = require("node:test");
const { JSDOM } = require("jsdom");
const { buildDesignModeScript } = require("../src/designModeScript");

it("identical Code picks carry unique selectors and a page without query or fragment", () => {
  const { window } = new JSDOM(
    '<main><code class="label">Code</code><code class="label">Code</code></main>',
    {
      url: "https://example.test/preview?view=1#details",
      runScripts: "outside-only",
    },
  );
  const log = [];
  window.console.log = (value) => log.push(value);
  const matches = window.Element.prototype.matches;
  window.Element.prototype.matches = function (selector) {
    return selector === ":popover-open" ? false : matches.call(this, selector);
  };
  window.HTMLElement.prototype.showPopover = () => {};
  window.HTMLElement.prototype.hidePopover = () => {};
  let picked;
  window.document.elementFromPoint = () => picked;
  window.eval(buildDesignModeScript("location"));
  try {
    const nodes = window.document.querySelectorAll("code");
    for (const node of nodes) {
      picked = node;
      node.getBoundingClientRect = () => ({
        x: 0,
        y: 0,
        left: 0,
        top: 0,
        right: 10,
        bottom: 10,
        width: 10,
        height: 10,
      });
      node.dispatchEvent(new window.MouseEvent("click", { bubbles: true, cancelable: true }));
    }
    const marker = "__omni_location_element_select__";
    const payloads = log
      .filter((x) => x.startsWith(marker))
      .map((x) => JSON.parse(x.slice(marker.length)));
    assert.equal(payloads.length, 2);
    assert.notEqual(payloads[0].selector, payloads[1].selector);
    for (let i = 0; i < nodes.length; i++) {
      assert.equal(window.document.querySelectorAll(payloads[i].selector).length, 1);
      assert.equal(window.document.querySelector(payloads[i].selector), nodes[i]);
      assert.equal(payloads[i].pageUrl, "https://example.test/preview");
    }
  } finally {
    window["__omniDisableDesignMode"]();
    window.close();
  }
});
