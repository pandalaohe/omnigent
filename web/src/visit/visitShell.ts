// Visitor shell: frames the shared HTML page and lets a visitor post one
// comment at a time on a text selection or the whole page. Plain DOM (no
// React) so the visit entry imports only leaf modules; the bridge protocol
// and the anchor mapping are shared with the owner-side viewer.
//
// Visitors never see other comments: the shell sends no `setComments` state
// into the frame and renders no list.

import { stripInjectedScripts } from "@/lib/stripInjectedScripts";
import {
  BRIDGE_MSG,
  BRIDGE_SOURCE,
  findAnchorInSource,
  parseBridgeMessage,
  type BridgeRect,
} from "@/shell/htmlCommentBridge";
import { artifactCommentsEndpoint, framePagePath, type VisitConfig } from "./visitConfig";

/** The window operations the shell uses; tests stand in for `location`. */
export interface VisitShellWindow {
  location: { reload: () => void };
  sessionStorage: Storage;
  setTimeout: Window["setTimeout"];
  clearTimeout: Window["clearTimeout"];
}

/** Handle for teardown: drops the pending ready timer and the bridge channel. */
export interface VisitShell {
  destroy: () => void;
}

/** How long after a frame load we wait for the bridge's `ready`. */
const BRIDGE_READY_TIMEOUT_MS = 4000;
const MAX_NAME = 40;
const MAX_BODY = 4000;
const DRAFT_PREFIX = "omni-visit-draft:";

const UNAVAILABLE_MESSAGE = "Comments are unavailable on this page";
const RECEIVED_MESSAGE = "Received";
const DISABLED_MESSAGE = "Comments are turned off for this page";
const GONE_MESSAGE = "This link is no longer available";
const RATE_LIMITED_MESSAGE = "Too many comments from this link, try again later";
const NETWORK_ERROR_MESSAGE = "Couldn't send your comment. Check your connection and try again.";
const RETRY_MESSAGE = "Couldn't send your comment. Try again.";

interface Anchor {
  text: string;
  occ: number;
}

/** Where the open composer sends: the page the frame showed when it opened. */
interface ComposerTarget {
  path: string;
  url: string;
  anchor: Anchor | null;
}

/** An unsent comment kept in sessionStorage until a 201. */
interface VisitDraft {
  path: string;
  url: string;
  text: string;
  name: string;
  anchor_content: string | null;
  occ: number;
}

function element<K extends keyof HTMLElementTagNameMap>(
  doc: Document,
  tag: K,
  props: Partial<HTMLElementTagNameMap[K]> = {},
): HTMLElementTagNameMap[K] {
  return Object.assign(doc.createElement(tag), props);
}

const STYLE = `
html, body { margin: 0; height: 100%; }
#omni-visit-frame { position: fixed; inset: 0; width: 100%; height: 100%; border: 0; }
#omni-visit-status, #omni-visit-unavailable {
  position: fixed; top: 8px; left: 50%; transform: translateX(-50%); z-index: 10;
  max-width: calc(100vw - 32px); padding: 6px 12px; border-radius: 6px;
  background: rgba(17, 24, 39, 0.85); color: #fff; font: 13px/1.4 system-ui, sans-serif;
}
#omni-visit-comment, #omni-visit-selection-comment {
  position: fixed; z-index: 10; padding: 6px 12px; border: 1px solid #d1d5db;
  border-radius: 6px; background: #fff; color: #111827; cursor: pointer;
  font: 13px/1.4 system-ui, sans-serif; box-shadow: 0 1px 3px rgba(0, 0, 0, 0.15);
}
#omni-visit-comment { right: 16px; bottom: 16px; }
#omni-visit-selection-comment { transform: translateY(-100%); }
#omni-visit-form {
  position: fixed; right: 16px; bottom: 16px; z-index: 11; width: 320px;
  display: flex; flex-direction: column; gap: 8px; padding: 12px;
  border: 1px solid #d1d5db; border-radius: 8px; background: #fff;
  font: 13px/1.4 system-ui, sans-serif; box-shadow: 0 4px 16px rgba(0, 0, 0, 0.2);
}
#omni-visit-anchor { max-height: 80px; overflow: auto; font-style: italic; color: #4b5563; }
#omni-visit-body { min-height: 72px; resize: vertical; font: inherit; }
#omni-visit-error { color: #b91c1c; }
#omni-visit-submit, #omni-visit-cancel { padding: 4px 10px; font: inherit; cursor: pointer; }
`;

/**
 * Mount the visitor shell for *config* under *root*.
 *
 * @param root The container the shell renders into (the document body).
 * @param config The parsed `#omni-visit-config`.
 * @param win The window operations the shell uses.
 */
export function mountVisitShell(
  root: HTMLElement,
  config: VisitConfig,
  win: VisitShellWindow = window,
): VisitShell {
  const doc = root.ownerDocument;

  doc.head.appendChild(element(doc, "style", { textContent: STYLE }));

  const frame = element(doc, "iframe", { id: "omni-visit-frame", title: "Shared page" });
  frame.src = config.frameUrl;
  root.appendChild(frame);

  const status = element(doc, "div", { id: "omni-visit-status", hidden: true });
  const unavailable = element(doc, "div", {
    id: "omni-visit-unavailable",
    hidden: true,
    textContent: UNAVAILABLE_MESSAGE,
  });
  root.append(status, unavailable);

  const commentButton = config.commentsEnabled
    ? element(doc, "button", { id: "omni-visit-comment", type: "button", hidden: true })
    : null;
  if (commentButton) {
    commentButton.textContent = "Comment";
    root.appendChild(commentButton);
  }

  const selectionButton = config.commentsEnabled
    ? element(doc, "button", { id: "omni-visit-selection-comment", type: "button", hidden: true })
    : null;
  if (selectionButton) {
    selectionButton.textContent = "Comment";
    root.appendChild(selectionButton);
  }

  const form = element(doc, "form", { id: "omni-visit-form", hidden: true });
  const anchorPreview = element(doc, "div", { id: "omni-visit-anchor", hidden: true });
  const nameInput = element(doc, "input", {
    id: "omni-visit-name",
    type: "text",
    maxLength: MAX_NAME,
    placeholder: "Your name (optional)",
  });
  const bodyInput = element(doc, "textarea", {
    id: "omni-visit-body",
    maxLength: MAX_BODY,
    placeholder: "Your comment",
    required: true,
  });
  const errorText = element(doc, "div", { id: "omni-visit-error", hidden: true });
  const submitButton = element(doc, "button", { id: "omni-visit-submit", type: "submit" });
  submitButton.textContent = "Send";
  const cancelButton = element(doc, "button", { id: "omni-visit-cancel", type: "button" });
  cancelButton.textContent = "Cancel";
  form.append(anchorPreview, nameInput, bodyInput, errorText, submitButton, cancelButton);
  root.appendChild(form);

  let channel: MessageChannel | null = null;
  let readyTimer: number | null = null;
  let ready = false;
  let sending = false;
  let page = { path: config.path, url: config.frameUrl };
  let selection: (Anchor & { rect: BridgeRect }) | null = null;
  let composerTarget: ComposerTarget | null = null;

  const clearReadyTimer = () => {
    if (readyTimer !== null) {
      win.clearTimeout(readyTimer);
      readyTimer = null;
    }
  };

  const setError = (message: string | null) => {
    errorText.textContent = message ?? "";
    errorText.hidden = message === null;
  };

  const setStatus = (message: string | null) => {
    status.textContent = message ?? "";
    status.hidden = message === null;
  };

  const updateAffordances = () => {
    const composerOpen = !form.hidden;
    if (commentButton) commentButton.hidden = !ready || composerOpen;
    if (selectionButton) {
      selectionButton.hidden = !ready || selection === null || composerOpen;
    }
  };

  const draftKey = (path: string) => `${DRAFT_PREFIX}${config.token}:${path}`;

  const storeDraft = () => {
    if (!composerTarget || form.hidden) return;
    const key = draftKey(composerTarget.path);
    try {
      if (!bodyInput.value.trim()) {
        win.sessionStorage.removeItem(key);
        return;
      }
      const draft: VisitDraft = {
        path: composerTarget.path,
        url: composerTarget.url,
        text: bodyInput.value,
        name: nameInput.value,
        anchor_content: composerTarget.anchor?.text ?? null,
        occ: composerTarget.anchor?.occ ?? 0,
      };
      win.sessionStorage.setItem(key, JSON.stringify(draft));
    } catch {
      // Session storage can be unavailable (private mode, quota); losing the
      // draft safety net must not break commenting.
    }
  };

  const readStoredDraft = (): VisitDraft | null => {
    const prefix = `${DRAFT_PREFIX}${config.token}:`;
    try {
      for (let i = 0; i < win.sessionStorage.length; i += 1) {
        const key = win.sessionStorage.key(i);
        if (!key || !key.startsWith(prefix)) continue;
        const parsed = JSON.parse(win.sessionStorage.getItem(key) ?? "null") as unknown;
        if (
          typeof parsed === "object" &&
          parsed !== null &&
          typeof (parsed as VisitDraft).path === "string" &&
          typeof (parsed as VisitDraft).url === "string" &&
          typeof (parsed as VisitDraft).text === "string" &&
          typeof (parsed as VisitDraft).name === "string" &&
          ((parsed as VisitDraft).anchor_content === null ||
            typeof (parsed as VisitDraft).anchor_content === "string") &&
          typeof (parsed as VisitDraft).occ === "number"
        ) {
          return parsed as VisitDraft;
        }
      }
    } catch {
      // Unreadable storage simply has no draft to restore.
    }
    return null;
  };

  const openComposer = (target: ComposerTarget, draft?: VisitDraft) => {
    composerTarget = target;
    nameInput.value = draft?.name ?? "";
    bodyInput.value = draft?.text ?? "";
    anchorPreview.textContent = target.anchor ? `“${target.anchor.text}”` : "";
    anchorPreview.hidden = target.anchor === null;
    setError(null);
    form.hidden = false;
    updateAffordances();
    bodyInput.focus();
  };

  const closeComposer = (clearFields: boolean) => {
    form.hidden = true;
    composerTarget = null;
    if (clearFields) {
      nameInput.value = "";
      bodyInput.value = "";
    }
    updateAffordances();
  };

  const fetchPageSource = async (url: string): Promise<string | null> => {
    try {
      // oxlint-disable-next-line no-restricted-globals -- the artifact GET is token-authorized; identity/embed headers must not be attached.
      const response = await fetch(url);
      if (!response.ok) return null;
      return stripInjectedScripts(await response.text(), config.nonce);
    } catch {
      return null;
    }
  };

  const readReason = async (response: Response): Promise<string | null> => {
    try {
      const data = (await response.json()) as { reason?: unknown };
      return typeof data.reason === "string" ? data.reason : null;
    } catch {
      return null;
    }
  };

  const finishSend = () => {
    sending = false;
    submitButton.disabled = false;
    submitButton.textContent = "Send";
  };

  const submit = async () => {
    const target = composerTarget;
    const text = bodyInput.value.trim();
    if (!target || !text || sending) return;
    sending = true;
    setError(null);
    submitButton.disabled = true;
    submitButton.textContent = "Sending…";

    let startIndex = 0;
    let endIndex = 0;
    let anchorContent: string | undefined;
    if (target.anchor) {
      // The frame's page source anchors offsets to the file the agent edits;
      // when it cannot be read, the anchor text still travels as the locator.
      anchorContent = target.anchor.text;
      const source = await fetchPageSource(target.url);
      const offsets =
        source === null ? null : findAnchorInSource(source, target.anchor.text, target.anchor.occ);
      if (offsets) {
        startIndex = offsets.start_index;
        endIndex = offsets.end_index;
      }
    }

    const payload: Record<string, unknown> = {
      token: config.token,
      grant: config.grant,
      path: target.path,
      body: text,
      start_index: startIndex,
      end_index: endIndex,
      name: nameInput.value.trim() || null,
    };
    if (anchorContent !== undefined) payload.anchor_content = anchorContent;

    let response: Response;
    try {
      // oxlint-disable-next-line no-restricted-globals -- a visitor page has no identity or embed transport; the token and grant in the body are the authority.
      response = await fetch(artifactCommentsEndpoint(config.frameUrl), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
    } catch {
      finishSend();
      setError(NETWORK_ERROR_MESSAGE);
      return;
    }

    if (response.status === 201) {
      try {
        win.sessionStorage.removeItem(draftKey(target.path));
      } catch {
        // The comment landed; failing to forget the draft is harmless.
      }
      closeComposer(true);
      setStatus(RECEIVED_MESSAGE);
    } else if (response.status === 403) {
      const reason = await readReason(response);
      if (reason === "disabled") {
        setError(DISABLED_MESSAGE);
      } else {
        // A stale grant or a closed gate: keep the draft and take the visitor
        // through the gate again at the top level.
        storeDraft();
        win.location.reload();
        return;
      }
    } else if (response.status === 410) {
      setError(GONE_MESSAGE);
    } else if (response.status === 429) {
      setError(RATE_LIMITED_MESSAGE);
    } else {
      setError(RETRY_MESSAGE);
    }
    finishSend();
  };

  const positionSelectionButton = () => {
    if (!selectionButton || !selection) return;
    const rect = frame.getBoundingClientRect();
    selectionButton.style.left = `${rect.left + selection.rect.left}px`;
    selectionButton.style.top = `${rect.top + selection.rect.top - 6}px`;
  };

  const handleMessage = (raw: unknown) => {
    const message = parseBridgeMessage(raw, config.nonce);
    if (!message) return;
    if (message.type === BRIDGE_MSG.ready) {
      ready = true;
      clearReadyTimer();
      unavailable.hidden = true;
      if (message.pathname !== undefined) {
        const path = framePagePath(message.pathname);
        if (path !== null) page = { path, url: message.pathname };
      }
      updateAffordances();
      return;
    }
    if (message.type === BRIDGE_MSG.selection) {
      if (!ready) return;
      selection = { text: message.text, occ: message.occ, rect: message.rect };
      positionSelectionButton();
      updateAffordances();
      return;
    }
    if (message.type === BRIDGE_MSG.selectionCleared) {
      selection = null;
      updateAffordances();
    }
  };

  const onFrameLoad = () => {
    const frameWindow = frame.contentWindow;
    if (!frameWindow) return;
    ready = false;
    selection = null;
    updateAffordances();
    unavailable.hidden = true;
    channel?.port1.close();
    channel = new MessageChannel();
    channel.port1.onmessage = (event: MessageEvent) => handleMessage(event.data);
    // targetOrigin "*" is required: the frame's sandbox CSP gives it an opaque
    // origin. The transferred port and the nonce are the trust mechanism.
    frameWindow.postMessage(
      { source: BRIDGE_SOURCE, nonce: config.nonce, type: BRIDGE_MSG.init, visit: true },
      "*",
      [channel.port2],
    );
    clearReadyTimer();
    if (config.commentsEnabled) {
      readyTimer = win.setTimeout(() => {
        if (!ready) unavailable.hidden = false;
      }, BRIDGE_READY_TIMEOUT_MS);
    }
  };

  if (commentButton) {
    commentButton.addEventListener("click", () => {
      openComposer({ path: page.path, url: page.url, anchor: null });
    });
  }
  if (selectionButton) {
    selectionButton.addEventListener("click", () => {
      if (selection) {
        openComposer({
          path: page.path,
          url: page.url,
          anchor: { text: selection.text, occ: selection.occ },
        });
      }
    });
  }
  cancelButton.addEventListener("click", () => closeComposer(false));
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    void submit();
  });
  for (const input of [nameInput, bodyInput]) {
    input.addEventListener("input", storeDraft);
  }
  frame.addEventListener("load", onFrameLoad);

  const restored = readStoredDraft();
  if (restored) {
    openComposer(
      {
        path: restored.path,
        url: restored.url,
        anchor:
          restored.anchor_content === null
            ? null
            : { text: restored.anchor_content, occ: restored.occ },
      },
      restored,
    );
  }

  return {
    destroy() {
      clearReadyTimer();
      frame.removeEventListener("load", onFrameLoad);
      channel?.port1.close();
      channel = null;
    },
  };
}
