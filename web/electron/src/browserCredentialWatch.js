"use strict";

const { CREDENTIAL_FIELD_JS } = require("./browserActionScript.mjs");

const WATCH_JS = `(() => {
  if (window.__omni_credential_watch__) return;
  const isCredentialField = ${CREDENTIAL_FIELD_JS};
  function mark() {
    for (const el of document.querySelectorAll('input, textarea, select')) {
      if (isCredentialField(el)) el.setAttribute('data-omnigent-credential', '');
    }
  }
  const observer = new MutationObserver((records) => {
    for (const record of records) {
      if (record.attributeName === 'type' && record.oldValue?.toLowerCase() === 'password') {
        record.target.setAttribute('data-omnigent-credential', '');
      }
    }
    mark();
  });
  observer.observe(document, { subtree: true, childList: true, attributes: true,
    attributeOldValue: true, attributeFilter: ['type', 'name', 'id', 'autocomplete', 'style', 'class'] });
  mark();
  window.__omni_credential_watch__ = observer;
})()`;

const SHOWING_JS = `(() => {
  if (!window.__omni_credential_watch__) return 'unknown';
  const isCredentialField = ${CREDENTIAL_FIELD_JS};
  for (const el of document.querySelectorAll('input, textarea, select')) {
    if (!isCredentialField(el) || !el.value) continue;
    const style = window.getComputedStyle(el);
    if (el.type === 'password' || (style.webkitTextSecurity && style.webkitTextSecurity !== 'none')) continue;
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') continue;
    const rect = el.getBoundingClientRect();
    if (rect.width > 0 && rect.height > 0) return 'yes';
  }
  return 'no';
})()`;

function attachCredentialWatch(entry) {
  const wc = entry.view.webContents;
  const install = () => {
    for (const frame of wc.mainFrame?.framesInSubtree || []) {
      try {
        Promise.resolve(frame.executeJavaScript(WATCH_JS)).catch(() => {});
      } catch {
        /* frame navigated or was destroyed */
      }
    }
  };
  wc.on("dom-ready", install);
  wc.on("did-frame-finish-load", install);
}

async function passwordIsShowing(wc) {
  try {
    const main = wc.mainFrame;
    if (!main) return "unknown";
    const frames = main.framesInSubtree;
    if (!frames?.includes(main)) return "unknown";
    const states = await Promise.all(
      frames.map(async (frame) => {
        try {
          const state = await frame.executeJavaScript(SHOWING_JS);
          return ["yes", "no", "unknown"].includes(state) ? state : "unknown";
        } catch {
          return frame === main ? "unknown" : "no";
        }
      }),
    );
    if (states.includes("yes")) return "yes";
    return states.includes("unknown") ? "unknown" : "no";
  } catch {
    return "unknown";
  }
}

async function captureCredentialSafePage(wc, rect) {
  const error =
    "Cannot capture this page while a password is visible or its protection is unavailable.";
  if ((await passwordIsShowing(wc)) !== "no") throw new Error(error);
  const image = await (rect ? wc.capturePage(rect) : wc.capturePage());
  if ((await passwordIsShowing(wc)) !== "no") throw new Error(error);
  return image;
}

module.exports = {
  WATCH_JS,
  SHOWING_JS,
  attachCredentialWatch,
  passwordIsShowing,
  captureCredentialSafePage,
};
