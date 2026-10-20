// Visitor shell: frames the shared HTML page and lets a visitor post one
// comment at a time on a text selection or the whole page. Plain DOM (no
// React) so the visit entry imports only leaf modules; the bridge protocol
// and the anchor mapping are shared with the owner-side viewer.
//
// Visitors never see other comments: the shell sends no `setComments` state
// into the frame and renders no list.

import {
  BRIDGE_MSG,
  BRIDGE_SOURCE,
  parseBridgeMessage,
  type BridgeRect,
} from "@/shell/htmlCommentBridge";
import {
  decodeRenderedTextAnchor,
  encodeRenderedTextAnchor,
  type RenderedTextAnchor,
} from "@/shell/renderedTextAnchor";
import {
  ELEMENT_ANCHOR_PREFIX,
  encodeElementAnchor,
  decodeElementAnchor,
  type ElementAnchorV1,
} from "@/shell/annotationAnchor";
import {
  ANNOTATE_MSG,
  ANNOTATE_SOURCE,
  parseAnnotateMessage,
  type AnnotatePickedScreenshot,
} from "@/shell/htmlAnnotateBridge";
import { loadRuntimeSources, type AnnotateRuntimePart } from "@/shell/annotate/runtimeSources";
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
const SCREENSHOT_QUOTA_MESSAGE =
  "This link has reached its 100 screenshot limit. Your draft is still here.";
const NETWORK_ERROR_MESSAGE = "Couldn't send your comment. Check your connection and try again.";
const RETRY_MESSAGE = "Couldn't send your comment. Try again.";

interface Anchor {
  text: string;
  occ: number;
  prefix: string;
  suffix: string;
  selector?: string;
}

/** Where the open composer sends: the page the frame showed when it opened. */
interface ComposerTarget {
  path: string;
  anchor: Anchor | null;
  legacyQuote?: string;
  elementAnchor?: ElementAnchorV1;
  screenshot?: AnnotatePickedScreenshot | null;
  generation?: number;
}

/** An unsent comment kept in sessionStorage until a 201. */
interface VisitDraft {
  path: string;
  text: string;
  name: string;
  anchor_content: string | null;
  legacyQuote?: string;
  screenshot?: AnnotatePickedScreenshot | null;
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
/* The form's own display rule would otherwise beat the UA [hidden] rule. */
[hidden] { display: none !important; }
#omni-visit-frame { position: fixed; inset: 0; width: 100%; height: 100%; border: 0; }
#omni-visit-status, #omni-visit-unavailable {
  position: fixed; top: 8px; left: 50%; transform: translateX(-50%); z-index: 10;
  max-width: calc(100vw - 32px); padding: 6px 12px; border-radius: 6px;
  background: rgba(17, 24, 39, 0.85); color: #fff; font: 13px/1.4 system-ui, sans-serif;
}
#omni-visit-comment, #omni-visit-selection-comment, #omni-visit-annotate {
  position: fixed; z-index: 10; padding: 6px 12px; border: 1px solid #d1d5db;
  border-radius: 6px; background: #fff; color: #111827; cursor: pointer;
  font: 13px/1.4 system-ui, sans-serif; box-shadow: 0 1px 3px rgba(0, 0, 0, 0.15);
}
#omni-visit-comment { right: 16px; bottom: 16px; }
#omni-visit-annotate { right: 112px; bottom: 16px; }
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
#omni-visit-expiry { color: #4b5563; }
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
  const annotateButton = config.commentsEnabled
    ? element(doc, "button", { id: "omni-visit-annotate", type: "button", hidden: true })
    : null;
  if (annotateButton) {
    annotateButton.textContent = "Annotate";
    root.appendChild(annotateButton);
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
  const expiryText = element(doc, "div", {
    id: "omni-visit-expiry",
    hidden: true,
    textContent: "Screenshots expire after 30 days; notes and element details remain.",
  });
  const submitButton = element(doc, "button", { id: "omni-visit-submit", type: "submit" });
  submitButton.textContent = "Send";
  const cancelButton = element(doc, "button", { id: "omni-visit-cancel", type: "button" });
  cancelButton.textContent = "Cancel";
  form.append(
    anchorPreview,
    nameInput,
    bodyInput,
    expiryText,
    errorText,
    submitButton,
    cancelButton,
  );
  root.appendChild(form);

  let channel: MessageChannel | null = null;
  let annotateChannel: MessageChannel | null = null;
  let annotateReady = false;
  let annotateOn = false;
  let frameGeneration = 0;
  const runtimeLoaded: Record<AnnotateRuntimePart, boolean> = { core: false, pick: false };
  const runtimeWaiters: Record<AnnotateRuntimePart, ((ok: boolean) => void) | undefined> = {
    core: undefined,
    pick: undefined,
  };
  let readyTimer: number | null = null;
  let ready = false;
  let sending = false;
  let page = { path: config.path };
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
    if (annotateButton) {
      annotateButton.hidden = !ready || !annotateReady || composerOpen;
      annotateButton.textContent = annotateOn ? "Done annotating" : "Annotate";
    }
    if (selectionButton) {
      selectionButton.hidden = !ready || selection === null || composerOpen;
    }
  };

  const draftKey = (path: string) => `${DRAFT_PREFIX}${config.token}:${path}`;

  const storeDraft = () => {
    if (!composerTarget || form.hidden) return;
    const key = draftKey(composerTarget.path);
    let draft: VisitDraft | undefined;
    try {
      if (!bodyInput.value.trim()) {
        win.sessionStorage.removeItem(key);
        return;
      }
      draft = {
        path: composerTarget.path,
        text: bodyInput.value,
        name: nameInput.value,
        anchor_content: composerTarget.elementAnchor
          ? encodeElementAnchor(composerTarget.elementAnchor)
          : composerTarget.anchor
            ? encodeRenderedTextAnchor({ v: 1, kind: "html", ...composerTarget.anchor })
            : null,
        ...(composerTarget.elementAnchor ? { screenshot: composerTarget.screenshot } : {}),
        ...(composerTarget.legacyQuote ? { legacyQuote: composerTarget.legacyQuote } : {}),
      };
      win.sessionStorage.setItem(key, JSON.stringify(draft));
    } catch {
      if (composerTarget.elementAnchor) {
        if (draft) {
          try {
            win.sessionStorage.setItem(key, JSON.stringify({ ...draft, screenshot: null }));
          } catch {
            // Storage may also refuse the smaller text/DOM draft.
          }
        }
        setError("This screenshot draft is kept here, but may be lost on reload.");
      }
    }
  };

  const readStoredDraft = (): VisitDraft | null => {
    const prefix = `${DRAFT_PREFIX}${config.token}:`;
    try {
      for (let i = 0; i < win.sessionStorage.length; i += 1) {
        const key = win.sessionStorage.key(i);
        if (!key || !key.startsWith(prefix)) continue;
        const parsed = JSON.parse(win.sessionStorage.getItem(key) ?? "null") as unknown;
        if (typeof parsed !== "object" || parsed === null) continue;
        const draft = parsed as VisitDraft;
        if (
          typeof draft.path !== "string" ||
          typeof draft.text !== "string" ||
          typeof draft.name !== "string" ||
          draft.text.length > MAX_BODY ||
          draft.name.length > MAX_NAME
        )
          continue;
        if (draft.anchor_content === null) {
          if (
            draft.legacyQuote !== undefined &&
            (typeof draft.legacyQuote !== "string" || draft.legacyQuote.length > 16000)
          )
            continue;
          return draft;
        }
        if (typeof draft.anchor_content !== "string") continue;
        if (decodeElementAnchor(draft.anchor_content)) return draft;
        if (draft.anchor_content.startsWith(ELEMENT_ANCHOR_PREFIX)) continue;
        if (decodeRenderedTextAnchor(draft.anchor_content)?.kind === "html") return draft;
        // Older drafts retained a quote and occurrence but no reliable DOM
        // context. Keep the writing and make the whole-page conversion visible.
        if (draft.anchor_content.length <= 16000) {
          return {
            path: draft.path,
            text: draft.text,
            name: draft.name,
            anchor_content: null,
            legacyQuote: draft.anchor_content,
          };
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
    anchorPreview.textContent = target.anchor
      ? `“${target.anchor.text}”`
      : target.elementAnchor
        ? target.elementAnchor.target.label || "Selected element"
        : target.legacyQuote
          ? `Original selection “${target.legacyQuote}” cannot be located. This comment will be about the whole page.`
          : "";
    anchorPreview.hidden = target.anchor === null && !target.legacyQuote && !target.elementAnchor;
    expiryText.hidden = !target.elementAnchor;
    setError(null);
    form.hidden = false;
    updateAffordances();
    bodyInput.focus();
  };

  const closeComposer = (clearFields: boolean) => {
    if (composerTarget?.elementAnchor) {
      annotateChannel?.port1.postMessage({
        source: ANNOTATE_SOURCE,
        nonce: config.nonce,
        type: ANNOTATE_MSG.pickDone,
      });
    }
    form.hidden = true;
    composerTarget = null;
    if (clearFields) {
      nameInput.value = "";
      bodyInput.value = "";
    }
    updateAffordances();
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

    const startIndex = 0;
    const endIndex = 0;
    let anchorContent: string | undefined;
    if (target.anchor) {
      anchorContent =
        encodeRenderedTextAnchor({ v: 1, kind: "html", ...target.anchor }) ?? undefined;
      if (!anchorContent) {
        sending = false;
        submitButton.disabled = false;
        submitButton.textContent = "Send";
        setError("This text selection is too long to comment on");
        return;
      }
    }
    if (target.elementAnchor) {
      if (target.generation !== frameGeneration) {
        finishSend();
        setError("The page changed. Choose the element again.");
        return;
      }
      anchorContent = encodeElementAnchor(target.elementAnchor) ?? undefined;
      if (!anchorContent) {
        finishSend();
        setError("This element cannot be annotated.");
        return;
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
    if (target.elementAnchor && target.screenshot) {
      payload.screenshot = {
        data_url: target.screenshot.dataUrl,
        width: target.screenshot.width,
        height: target.screenshot.height,
      };
    }

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
      setStatus(
        target.elementAnchor ? "Received. Screenshot expires after 30 days." : RECEIVED_MESSAGE,
      );
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
      setError(
        (await readReason(response)) === "screenshot_quota"
          ? SCREENSHOT_QUOTA_MESSAGE
          : RATE_LIMITED_MESSAGE,
      );
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
        if (path !== null) page = { path };
      }
      updateAffordances();
      return;
    }
    if (message.type === BRIDGE_MSG.selection) {
      if (!ready) return;
      selection = {
        text: message.text,
        occ: message.occ,
        prefix: message.prefix,
        suffix: message.suffix,
        selector: message.selector,
        rect: message.rect,
      };
      positionSelectionButton();
      updateAffordances();
      return;
    }
    if (message.type === BRIDGE_MSG.selectionCleared) {
      selection = null;
      updateAffordances();
    }
  };

  const postAnnotate = (type: string, extra: Record<string, unknown> = {}) => {
    annotateChannel?.port1.postMessage({
      source: ANNOTATE_SOURCE,
      nonce: config.nonce,
      type,
      ...extra,
    });
  };

  const ensureRuntime = async (part: AnnotateRuntimePart, generation: number): Promise<boolean> => {
    if (runtimeLoaded[part]) return true;
    try {
      const sources = await loadRuntimeSources(part);
      if (generation !== frameGeneration || !annotateReady) return false;
      return await new Promise<boolean>((resolve) => {
        const timer = win.setTimeout(() => {
          runtimeWaiters[part] = undefined;
          resolve(false);
        }, BRIDGE_READY_TIMEOUT_MS);
        runtimeWaiters[part] = (ok) => {
          win.clearTimeout(timer);
          runtimeLoaded[part] = ok;
          resolve(ok);
        };
        postAnnotate(ANNOTATE_MSG.loadRuntime, { part, sources });
      });
    } catch {
      return false;
    }
  };

  const handleAnnotateMessage = (raw: unknown) => {
    const message = parseAnnotateMessage(raw, config.nonce);
    if (!message) return;
    if (message.type === ANNOTATE_MSG.ready) {
      annotateReady = true;
      updateAffordances();
    } else if (message.type === ANNOTATE_MSG.runtimeLoaded) {
      runtimeWaiters[message.part]?.(message.ok);
      runtimeWaiters[message.part] = undefined;
    } else if (message.type === ANNOTATE_MSG.modeChanged) {
      if (!message.on) {
        annotateOn = false;
        if (composerTarget?.elementAnchor) closeComposer(false);
        updateAffordances();
      } else if (!annotateOn) {
        postAnnotate(ANNOTATE_MSG.setMode, { on: false });
      }
    } else if (message.type === ANNOTATE_MSG.picked) {
      if (!annotateOn || !ready || !form.hidden) return;
      openComposer({
        path: page.path,
        anchor: null,
        elementAnchor: { ...message.anchor, screenshot: null },
        screenshot: message.screenshot,
        generation: frameGeneration,
      });
    }
    // Frame shortcut and marker messages never enable mode or save a note.
  };

  const onFrameLoad = () => {
    const frameWindow = frame.contentWindow;
    if (!frameWindow) return;
    if (composerTarget?.elementAnchor) {
      if (frameGeneration === 0) composerTarget.generation = 1;
      else closeComposer(false);
    }
    frameGeneration += 1;
    annotateReady = false;
    annotateOn = false;
    runtimeLoaded.core = false;
    runtimeLoaded.pick = false;
    for (const part of ["core", "pick"] as const) {
      runtimeWaiters[part]?.(false);
      runtimeWaiters[part] = undefined;
    }
    annotateChannel?.port1.close();
    if (config.commentsEnabled) {
      annotateChannel = new MessageChannel();
      annotateChannel.port1.onmessage = (event: MessageEvent) => handleAnnotateMessage(event.data);
      frameWindow.postMessage(
        { source: ANNOTATE_SOURCE, nonce: config.nonce, type: ANNOTATE_MSG.init },
        "*",
        [annotateChannel.port2],
      );
    }
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
      openComposer({ path: page.path, anchor: null });
    });
  }
  if (annotateButton) {
    annotateButton.addEventListener("click", async () => {
      if (!ready || !annotateReady) return;
      if (annotateOn) {
        annotateOn = false;
        postAnnotate(ANNOTATE_MSG.pickDone);
        postAnnotate(ANNOTATE_MSG.setMode, { on: false });
        updateAffordances();
        return;
      }
      const generation = frameGeneration;
      if (
        !(await ensureRuntime("core", generation)) ||
        !(await ensureRuntime("pick", generation))
      ) {
        setStatus(UNAVAILABLE_MESSAGE);
        return;
      }
      if (generation !== frameGeneration) return;
      annotateOn = true;
      postAnnotate(ANNOTATE_MSG.setMode, { on: true });
      updateAffordances();
    });
  }
  if (selectionButton) {
    selectionButton.addEventListener("click", () => {
      if (selection) {
        openComposer({
          path: page.path,
          anchor: {
            text: selection.text,
            occ: selection.occ,
            prefix: selection.prefix,
            suffix: selection.suffix,
            selector: selection.selector,
          },
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
        legacyQuote: restored.legacyQuote,
        anchor:
          restored.anchor_content === null
            ? null
            : (decodeRenderedTextAnchor(restored.anchor_content) as RenderedTextAnchor | null),
        elementAnchor: decodeElementAnchor(restored.anchor_content) ?? undefined,
        screenshot: restored.screenshot,
        generation: frameGeneration,
      },
      restored,
    );
  }

  return {
    destroy() {
      clearReadyTimer();
      frame.removeEventListener("load", onFrameLoad);
      postAnnotate(ANNOTATE_MSG.pickDone);
      postAnnotate(ANNOTATE_MSG.setMode, { on: false });
      channel?.port1.close();
      annotateChannel?.port1.close();
      channel = null;
      annotateChannel = null;
    },
  };
}
