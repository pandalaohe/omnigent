"use strict";

// Self-contained: sandboxed preloads cannot require local modules.
function installCredentialWatch() {
  const watchedRoots = new WeakSet();
  const fields = new WeakMap();
  const identity = Array.from(crypto.getRandomValues(new Uint32Array(4))).join("-");
  let counter = 0;
  let hasCredentials = false;
  function credential(el) {
    const mask = getComputedStyle(el).webkitTextSecurity;
    return (
      el.hasAttribute("data-omnigent-credential") ||
      el.type === "password" ||
      /password|passwd|pwd|one-time-code|cc-/i.test([el.name, el.id, el.autocomplete].join(" ")) ||
      (!!mask && mask !== "none")
    );
  }
  function masked(el) {
    const mask = getComputedStyle(el).webkitTextSecurity;
    return el.type === "password" || (!!mask && mask !== "none");
  }
  function scan(root = document, shadow = false) {
    if (!watchedRoots.has(root)) {
      watchedRoots.add(root);
      observer.observe(root, {
        subtree: true,
        childList: true,
        attributes: true,
        attributeOldValue: true,
        attributeFilter: [
          "type",
          "name",
          "id",
          "autocomplete",
          "class",
          "style",
          "value",
          "hidden",
        ],
      });
    }
    for (const el of root.querySelectorAll("*")) {
      if (el.matches("input,textarea,select") && (credential(el) || shadow)) {
        hasCredentials = true;
        el.setAttribute("data-omnigent-credential", "");
        const state = masked(el);
        if (fields.has(el) && fields.get(el) !== state) counter++;
        fields.set(el, state);
      }
      if (el.shadowRoot) scan(el.shadowRoot, true);
    }
    const html = document.documentElement;
    if (html) {
      html.setAttribute("data-omnigent-credential-watch", identity);
      html.setAttribute("data-omnigent-credential-reveals", String(counter));
    }
  }
  const observer = new MutationObserver((records) => {
    for (const record of records) {
      if (record.attributeName === "type" && record.oldValue?.toLowerCase() === "password") {
        record.target.setAttribute("data-omnigent-credential", "");
      }
      // Count transitions that were reversed before this mutation batch ran as well.
      if (
        hasCredentials &&
        record.type === "attributes" &&
        (record.attributeName === "type" ||
          record.attributeName === "style" ||
          record.attributeName === "class" ||
          record.attributeName === "hidden")
      )
        counter++;
    }
    scan();
  });
  scan();
  function sample() {
    scan();
    requestAnimationFrame(sample);
  }
  requestAnimationFrame(sample);
  addEventListener("input", () => scan(), true);
  addEventListener("change", () => scan(), true);
}

if (typeof document !== "undefined") installCredentialWatch();
if (typeof module !== "undefined") module.exports = { installCredentialWatch };
