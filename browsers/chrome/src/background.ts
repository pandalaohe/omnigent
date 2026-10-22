import { CaptureQueue, captureCrop, matchesSender } from "./capture";
import { canPickUrl, exactKeys, pageUrl, parseSelection, randomId, record } from "./protocol";
import type { Activation, PanelState } from "./protocol";

const activations = new Map<number, Activation>();
const panels = new Set<chrome.runtime.Port>();
const queue = new CaptureQueue();
let state: PanelState = {
  message: "Invoke Pick page evidence from the Chrome toolbar or shortcut.",
};

function publish(next: PanelState): void {
  state = next;
  for (const port of panels) {
    try {
      port.postMessage(state);
    } catch {
      panels.delete(port);
    }
  }
}

function invalidate(tabId: number): void {
  const activation = activations.get(tabId);
  if (!activation) return;
  activation.valid = false;
  activations.delete(tabId);
  if (activation.documentId)
    void chrome.tabs
      .sendMessage(
        tabId,
        { type: "dispose", generation: activation.generation },
        { documentId: activation.documentId },
      )
      .catch(() => undefined);
}

/** Called directly by Chrome action, including the manifest's _execute_action shortcut. */
export function invoke(tab: chrome.tabs.Tab): void {
  if (tab.id === undefined) return;
  // Chrome's user gesture is lost across await; this must be the first API call.
  const opened = chrome.sidePanel.open({ tabId: tab.id });
  invalidate(tab.id);
  const activation: Activation = {
    tabId: tab.id,
    windowId: tab.windowId,
    documentId: "",
    generation: randomId(),
    valid: true,
  };
  activations.set(tab.id, activation);
  void activate(tab, activation, opened).catch(() => {
    if (activations.get(activation.tabId) !== activation) return;
    invalidate(activation.tabId);
    publish({
      message: "Cannot pick here. Open an HTTP(S) page and invoke Pick page evidence again.",
    });
  });
}

async function activate(
  tab: chrome.tabs.Tab,
  activation: Activation,
  opened: Promise<void>,
): Promise<void> {
  await opened;
  if (!activation.valid) return;
  if (!canPickUrl(tab.url ?? "")) throw new Error("Restricted page");
  // Content scripts must never inherit storage.local access when later pairing is added.
  await chrome.storage.local.setAccessLevel({
    accessLevel: "TRUSTED_CONTEXTS",
  });
  if (!activation.valid) return;
  const results = await chrome.scripting.executeScript({
    target: { tabId: activation.tabId, frameIds: [0] },
    world: "ISOLATED",
    files: ["picker.js"],
  });
  const result = results[0];
  if (results.length !== 1 || result?.frameId !== 0 || !result.documentId)
    throw new Error("No top-frame document");
  activation.documentId = result.documentId;
  if (!activation.valid) {
    await chrome.tabs
      .sendMessage(
        activation.tabId,
        { type: "dispose", generation: activation.generation },
        { documentId: activation.documentId },
      )
      .catch(() => undefined);
    return;
  }
  const response = record(
    await chrome.tabs.sendMessage(
      activation.tabId,
      { type: "activate", generation: activation.generation },
      { documentId: activation.documentId },
    ),
  );
  if (response.ok !== true || !activation.valid) throw new Error("Picker not ready");
  publish({
    ...state,
    message: "Pick an element, drag a region, or capture selected text. Nothing is uploaded.",
  });
}

chrome.action.onClicked.addListener(invoke);
chrome.tabs.onActivated.addListener(({ tabId, windowId }) => {
  for (const activation of activations.values())
    if (activation.windowId === windowId && activation.tabId !== tabId)
      invalidate(activation.tabId);
});
chrome.tabs.onUpdated.addListener((tabId, change) => {
  if (change.status === "loading" || change.url) invalidate(tabId);
});
chrome.tabs.onRemoved.addListener(invalidate);
chrome.windows.onFocusChanged.addListener((windowId) => {
  for (const activation of activations.values())
    if (activation.windowId !== windowId) invalidate(activation.tabId);
});

chrome.runtime.onConnect.addListener((port) => {
  if (
    port.name !== "annotation-preview" ||
    port.sender?.id !== chrome.runtime.id ||
    port.sender.tab ||
    port.sender.url !== chrome.runtime.getURL("sidepanel.html")
  ) {
    port.disconnect();
    return;
  }
  panels.add(port);
  publish(state);
  port.onDisconnect.addListener(() => {
    panels.delete(port);
  });
});

chrome.runtime.onMessage.addListener((input, sender, respond) => {
  const activation = sender.tab?.id === undefined ? undefined : activations.get(sender.tab.id);
  if (!activation) return;
  try {
    const message = record(input);
    exactKeys(message, ["type", "generation", "selection"]);
    if (message.type !== "pick" || !matchesSender(activation, sender, message.generation)) return;
    const selection = parseSelection(message.selection);
    if (selection.page.url !== pageUrl(sender.url ?? "")) throw new Error("Sender page mismatch");
    const preview = {
      capture_id: randomId(),
      captured_at: new Date().toISOString(),
      selection,
    };
    publish({ ...state, message: "Capturing visible evidence…" });
    void queue
      .run(async () => {
        if (!activation.valid)
          throw new Error("Source tab changed. Invoke Pick page evidence again.");
        return captureCrop(activation, selection, queue);
      })
      .then((crop) => {
        publish({
          message:
            "Preview only — review the image and page evidence. This build does not save or send annotations.",
          preview: { ...preview, ...crop },
        });
        respond({ ok: true });
      })
      .catch((error: unknown) => {
        const screenshotFailure =
          error instanceof Error ? error.message : "Screenshot failed. Select again.";
        publish({
          message: "Screenshot failed. Select again; no screenshot was saved.",
          preview: { ...preview, screenshotFailure },
        });
        respond({ ok: false });
      });
    return true;
  } catch {
    respond({ ok: false });
    return;
  }
});
