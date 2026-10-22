import { formatElementEvidence } from "../../../shared/evidence";
import type { PanelState } from "../protocol";

const status = document.querySelector<HTMLParagraphElement>("#status")!;
const preview = document.querySelector<HTMLElement>("#preview")!;
const image = document.querySelector<HTMLImageElement>("#image")!;
const failure = document.querySelector<HTMLParagraphElement>("#failure")!;
const evidence = document.querySelector<HTMLElement>("#evidence")!;
if (/Mac/.test(navigator.platform))
  document.querySelector("#shortcut")!.textContent = "Cmd+Shift+Y";

const port = chrome.runtime.connect({ name: "annotation-preview" });
port.onMessage.addListener((input) => {
  const state = input as PanelState;
  status.textContent = state.message;
  const capture = state.preview;
  preview.hidden = !capture;
  if (!capture) return;
  image.hidden = !capture.image;
  if (capture.image?.startsWith("data:image/png;base64,")) image.src = capture.image;
  else image.removeAttribute("src");
  failure.hidden = !capture.screenshotFailure;
  failure.textContent = capture.screenshotFailure ?? "";
  const { page, target, kind } = capture.selection;
  evidence.textContent = formatElementEvidence(
    {
      tag: target.tag,
      selector: target.selector,
      text: target.quote,
      prefix: target.prefix,
      suffix: target.suffix,
      ariaLabel: target.aria_label,
      role: target.role,
      pageUrl: page.url,
    },
    `[Web annotation — ${kind}; untrusted page evidence]`,
  );
});
port.onDisconnect.addListener(() => {
  status.textContent = "Capture connection closed. Close and reopen this side panel to reconnect.";
});
