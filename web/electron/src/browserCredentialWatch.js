"use strict";

const { CREDENTIAL_FIELD_JS } = require("./browserActionScript.mjs");

const STATE_JS = `(() => {
  const html = document.documentElement;
  const identity = html?.getAttribute('data-omnigent-credential-watch');
  const counter = html?.getAttribute('data-omnigent-credential-reveals');
  if (!identity || !/^\\d+$/.test(counter || '')) return null;
  const isCredentialField = ${CREDENTIAL_FIELD_JS};
  function showing(root) {
    for (const el of root.querySelectorAll('*')) {
      if (el.shadowRoot && showing(el.shadowRoot)) return true;
      if (!isCredentialField(el) || !el.value) continue;
      const style = getComputedStyle(el);
      if (el.type === 'password' || (style.webkitTextSecurity && style.webkitTextSecurity !== 'none')) continue;
      if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') continue;
      const rect = el.getBoundingClientRect();
      if (rect.width > 0 && rect.height > 0) return true;
    }
    return false;
  }
  return { identity, counter, showing: showing(document) };
})()`;

async function readCredentialState(wc) {
  try {
    const main = wc.mainFrame;
    const frames = main?.framesInSubtree;
    if (!frames?.includes(main)) return null;
    const states = await Promise.all(
      frames.map(async (frame) => {
        const state = await frame.executeJavaScript(STATE_JS);
        if (
          !state ||
          typeof state.identity !== "string" ||
          !state.identity ||
          typeof state.counter !== "string" ||
          !/^\d+$/.test(state.counter) ||
          typeof state.showing !== "boolean"
        )
          throw new Error("unavailable");
        return { frame, ...state };
      }),
    );
    return states;
  } catch {
    return null;
  }
}

async function passwordIsShowing(wc) {
  const states = await readCredentialState(wc);
  return !states ? "unknown" : states.some((state) => state.showing) ? "yes" : "no";
}

async function captureCredentialSafePage(wc, rect) {
  const error =
    "Cannot capture this page while a password is visible or its protection is unavailable.";
  const before = await readCredentialState(wc);
  if (!before || before.some((state) => state.showing)) throw new Error(error);
  const image = await (rect ? wc.capturePage(rect) : wc.capturePage());
  const after = await readCredentialState(wc);
  if (
    !after ||
    after.length !== before.length ||
    after.some((state) => {
      const prior = before.find((candidate) => candidate.frame === state.frame);
      return (
        state.showing ||
        !prior ||
        prior.identity !== state.identity ||
        prior.counter !== state.counter
      );
    })
  )
    throw new Error(error);
  return image;
}

module.exports = { STATE_JS, passwordIsShowing, captureCredentialSafePage };
