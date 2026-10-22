import { CaptureQueue, captureCrop, matchesSender } from "./capture";
import { canPickUrl, exactKeys, pageUrl, parseSelection, randomId, record } from "./protocol";
import type { Activation, PanelState } from "./protocol";
import { AnnotationApp } from "./application";
import { Connection } from "./auth";
import { TrayStore } from "./store";
import type { CapturePreview } from "./protocol";
import { identityKey } from "./model";
import type { Identity } from "./model";

const activations = new Map<number, Activation>();
const panels = new Set<chrome.runtime.Port>();
const queue = new CaptureQueue();
let state: PanelState = {
  message: "Invoke Pick page evidence from the Chrome toolbar or shortcut.",
};
let activeApplication: AnnotationApp | undefined;
let application: Promise<AnnotationApp> | undefined;
function getApplication(): Promise<AnnotationApp> {
  application ??= (async () => {
    const app = new AnnotationApp(new TrayStore(), new Connection(), (view) =>
      publish({ ...state, app: view, message: view.message }),
    );
    await app.initialize();
    activeApplication = app;
    return app;
  })();
  return application;
}
function panelSender(sender: chrome.runtime.MessageSender): boolean {
  return (
    sender.id === chrome.runtime.id &&
    !sender.tab &&
    sender.url === chrome.runtime.getURL("sidepanel.html")
  );
}

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
  void getApplication()
    .then((app) => app.publish())
    .catch(() =>
      publish({
        ...state,
        message:
          "Local storage could not open. Captures remain temporary; close and reopen the panel to try again.",
      }),
    );
  port.onDisconnect.addListener(() => {
    panels.delete(port);
  });
});

chrome.runtime.onMessage.addListener((input, sender, respond) => {
  if (panelSender(sender)) {
    try {
      const message = record(input);
      exactKeys(message, ["type", "command"]);
      if (message.type !== "panel-command") return;
      void getApplication()
        .then((app) => {
          const command = record(message.command);
          if (command.type === "savePreview") {
            const owner = app.identity();
            if (!owner || command.key !== identityKey(owner) || !state.preview)
              throw new Error("Capture or account changed. Review the current panel.");
            return app.capture(state.preview, owner).then(() => {
              publish({ ...state, preview: undefined });
            });
          }
          return app.handle(command);
        })
        .then(() => respond({ ok: true }))
        .catch((error: unknown) => {
          const message =
            error instanceof Error ? error.message : "Operation stopped. Local data is retained.";
          publish({ ...state, message });
          respond({ ok: false, error: message });
        });
      return true;
    } catch {
      respond({ ok: false, error: "Invalid panel command." });
      return;
    }
  }
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
    const captureOwner = activeApplication?.identity();
    if (captureOwner) Object.assign(preview, { ownerKey: identityKey(captureOwner) });
    const fullUrl =
      activeApplication?.fullUrlEnabled() && captureOwner
        ? pageUrl(sender.url ?? "", true)
        : undefined;
    const finish = async (full: CapturePreview, owner: Identity | undefined, ok: boolean) => {
      if (fullUrl && owner)
        full = {
          ...full,
          urlConsentKey: identityKey(owner),
          selection: {
            ...full.selection,
            page: { ...full.selection.page, url: fullUrl, include_query_fragment: true },
          },
        };
      publish({
        ...state,
        preview: full,
        message: ok
          ? "Capture preview ready; saving locally…"
          : "Screenshot failed. Review the retained evidence and choose explicitly whether to omit it.",
      });
      try {
        await (await getApplication()).capture(full, owner);
        publish({ ...state, preview: undefined });
      } catch (error) {
        publish({
          ...state,
          preview: full,
          message:
            error instanceof Error
              ? error.message
              : "Not saved locally. The temporary capture remains visible.",
        });
      }
      respond({ ok });
    };
    publish({ ...state, message: "Capturing visible evidence…" });
    void queue
      .run(async () => {
        if (!activation.valid)
          throw new Error("Source tab changed. Invoke Pick page evidence again.");
        return captureCrop(activation, selection, queue);
      })
      .then((crop) => finish({ ...preview, ...crop }, captureOwner, true))
      .catch((error: unknown) => {
        const screenshotFailure =
          error instanceof Error ? error.message : "Screenshot failed. Select again.";
        void finish({ ...preview, screenshotFailure }, captureOwner, false);
      });
    return true;
  } catch {
    respond({ ok: false });
    return;
  }
});
