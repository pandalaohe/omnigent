import { sanitizeField } from "../../../shared/evidence";
import { pageUrl } from "../protocol";
import type { Metrics, PageV1, Rect, TargetV1 } from "../protocol";

export const DIALOG_SELECTOR = 'dialog[open], [role="dialog"], [role="alertdialog"]';

export function isActiveTarget(element: Element): boolean {
  const dialog = element.closest(DIALOG_SELECTOR);
  return (
    element.isConnected &&
    element.getClientRects().length > 0 &&
    (!element.checkVisibility ||
      element.checkVisibility({
        opacityProperty: true,
        visibilityProperty: true,
      })) &&
    !element.closest("[inert], [hidden], dialog:not([open])") &&
    !dialog?.closest(
      '[aria-hidden="true"], [role="dialog"][data-state="closed"], [role="alertdialog"][data-state="closed"]',
    )
  );
}

export function nodeAddress(element: Element): string {
  if (element.getRootNode() !== document) return "";
  const parts: string[] = [];
  for (let node: Element | null = element; node; node = node.parentElement) {
    const tag = node.localName;
    if (!/^[a-z][a-z0-9-]*$/i.test(tag)) return "";
    let index = 1;
    for (
      let sibling = node.previousElementSibling;
      sibling;
      sibling = sibling.previousElementSibling
    ) {
      if (sibling.localName === tag) index++;
    }
    parts.unshift(`${tag}:nth-of-type(${index})`);
  }
  const selector = parts.join(" > ");
  if (selector.length > 700) return "";
  const matches = document.querySelectorAll(selector);
  return matches.length === 1 && matches[0] === element ? selector : "";
}

function visibleTextNode(node: Node): boolean {
  let element: Element | null = node.parentElement;
  while (element) {
    if (
      element.matches(
        'input,textarea,select,script,style,[contenteditable],[hidden],[inert],[aria-hidden="true"]',
      )
    )
      return false;
    const style = getComputedStyle(element);
    if (
      style.display === "none" ||
      style.visibility !== "visible" ||
      style.opacity === "0" ||
      style.contentVisibility === "hidden"
    )
      return false;
    const parent = element.parentElement;
    element =
      parent ??
      (element.getRootNode() instanceof ShadowRoot
        ? (element.getRootNode() as ShadowRoot).host
        : null);
  }
  return true;
}

/** Only visible text nodes; never read .value, outerHTML or fibers. */
export function visibleText(root: Node): string {
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  let text = "";
  while (walker.nextNode() && text.length < 4096) {
    if (visibleTextNode(walker.currentNode)) text += walker.currentNode.textContent ?? "";
  }
  return sanitizeField(text, 200);
}

function visibleRangeText(range: Range, cap: number, tail = false): string {
  const root = range.commonAncestorContainer;
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  let node: Node | null = root.nodeType === Node.TEXT_NODE ? root : walker.nextNode();
  let text = "";
  while (node) {
    if (range.intersectsNode(node) && visibleTextNode(node)) {
      const start = node === range.startContainer ? range.startOffset : 0;
      const end = node === range.endContainer ? range.endOffset : node.textContent?.length;
      text += (node.textContent ?? "").slice(start, end);
      if (tail) text = text.slice(-cap * 2);
      else if (sanitizeField(text, cap).length >= cap) break;
    }
    node = walker.nextNode();
  }
  const cleaned = sanitizeField(text, tail ? text.length : cap);
  return tail ? cleaned.slice(-cap) : cleaned;
}

export function elementTarget(element: Element): TargetV1 {
  const shadow = element.getRootNode() instanceof ShadowRoot;
  const unavailable = element.matches("iframe,object,embed");
  return {
    tag: sanitizeField(element.localName, 32),
    selector: shadow || unavailable ? "" : nodeAddress(element),
    quote: unavailable ? "" : visibleText(element),
    prefix: siblingContext(element.previousElementSibling, true),
    suffix: siblingContext(element.nextElementSibling, false),
    aria_label: sanitizeField(element.getAttribute("aria-label"), 200),
    role: sanitizeField(element.getAttribute("role"), 80),
  };
}

function siblingContext(element: Element | null, tail: boolean): string {
  if (!element) return "";
  const range = document.createRange();
  range.selectNodeContents(element);
  return visibleRangeText(range, 32, tail);
}

export function rectOf(rect: DOMRect | Rect): Rect {
  return { x: rect.x, y: rect.y, width: rect.width, height: rect.height };
}

export function pageEvidence(): PageV1 {
  return {
    url: pageUrl(location.href),
    title: sanitizeField(document.title),
    include_query_fragment: false,
  };
}

export function metrics(revision: number): Metrics {
  const viewport = window.visualViewport;
  // CSSOM gives viewport client dimensions on body in quirks mode, root otherwise.
  const client = document.compatMode === "BackCompat" ? document.body : document.documentElement;
  return {
    width: innerWidth,
    height: innerHeight,
    clientWidth: client.clientWidth,
    clientHeight: client.clientHeight,
    scrollX,
    scrollY,
    dpr: devicePixelRatio,
    visual: {
      x: viewport?.offsetLeft ?? 0,
      y: viewport?.offsetTop ?? 0,
      width: viewport?.width ?? client.clientWidth,
      height: viewport?.height ?? client.clientHeight,
      scale: viewport?.scale ?? 1,
    },
    revision,
    visible: document.visibilityState === "visible",
    url: pageUrl(location.href),
  };
}

export function selectedText(): { target: TargetV1; range: Range } {
  const selection = getSelection();
  if (!selection || selection.isCollapsed || selection.rangeCount !== 1)
    throw new Error("Select visible page text first, then click Capture selected text.");
  const range = selection.getRangeAt(0).cloneRange();
  const ancestor = range.commonAncestorContainer;
  const element = ancestor instanceof Element ? ancestor : ancestor.parentElement;
  if (
    !element ||
    element.closest("input,textarea,select,[contenteditable]") ||
    element.querySelector("input,textarea,select,[contenteditable]")
  ) {
    throw new Error("Select text outside form controls and hidden content.");
  }
  const target = elementTarget(element);
  const quote = visibleRangeText(range, 200);
  if (!quote) throw new Error("Selection is empty");
  // Context stays within the selected element, rather than dumping the document.
  const before = range.cloneRange();
  before.selectNodeContents(element);
  before.setEnd(range.startContainer, range.startOffset);
  const after = range.cloneRange();
  after.selectNodeContents(element);
  after.setStart(range.endContainer, range.endOffset);
  target.quote = quote;
  target.prefix = visibleRangeText(before, 32, true);
  target.suffix = visibleRangeText(after, 32);
  return { target, range };
}
