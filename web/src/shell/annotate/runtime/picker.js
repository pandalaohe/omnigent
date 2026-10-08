// In-frame pick runtime: annotation mode, hover outline, click pick, region
// select and the selected outline that posts `annotate:picked` (design §2.9).
// The note box is owner UI, so after a pick the frame only keeps the outline
// and ignores further picks until the parent posts `annotate:pickDone`. Loaded
// as the `pick` runtime part on first mode entry. The open-shadow hit test and
// the composed-parent walk are ports of react-grab's
// get-deep-element-at-point.ts and get-composed-parent-element.ts (MIT); see
// THIRD_PARTY_NOTICES.md.

var OVERLAY_HOST_ID = "__omni-annotate-host";
var PICK_MARGIN = 16; // capture crop padding around the picked rect
var CLICK_SLOP = 4; // pointer travel below this stays a click, not a drag

// Seam owned by the freeze slice: freeze.js replaces these hooks before picker
// is evaluated; the fallbacks keep the picker inert without it.
ns.freeze = ns.freeze || {
  on: function () {},
  off: function () {},
  openForHitTest: function () {},
  closeAfterHitTest: function () {},
};

var on = false;
var container = null; // picker UI inside the overlay shadow root
var outline = null;
var label = null;
var regionBox = null; // dashed box drawn while dragging a region
var selected = null; // solid outline kept on the picked rect until pickDone
var cursorStyle = null;
var pendingPick = false; // a picked message is out; ignore further picks
var hoverEl = null;
var downPoint = null; // pointerdown position of the pending click/drag
var dragging = false; // pointerdown travel crossed CLICK_SLOP
var lastPoint = null; // last pointer position seen in mode, for the freeze
var listeners = []; // [type, handler, viaCaptureSlot] capture-phase handlers
var capturePending = false; // one screenshot at a time
var modeGeneration = 0; // bumped on teardown so stale captures are dropped

// Page scripts share the realm and can dispatch events, so only the user's own
// input (isTrusted) may act.
function isTrustedAction(event) {
  return event.isTrusted === true;
}

var OUTLINE_STYLE =
  "position:absolute;pointer-events:none;box-sizing:border-box;display:none;z-index:1;" +
  "border:1.5px solid #c15f3c;background:rgba(193,95,60,0.08);";

var REGION_STYLE =
  "position:absolute;pointer-events:none;box-sizing:border-box;display:none;z-index:1;" +
  "border:1px dashed #c15f3c;background:rgba(193,95,60,0.10);";

var HOVER_LABEL_STYLE =
  "position:absolute;pointer-events:none;display:none;white-space:nowrap;z-index:2;" +
  "background:#c15f3c;color:#fff;padding:2px 6px;border-radius:3px;" +
  "font:11px/1.4 ui-monospace,SFMono-Regular,Menlo,monospace;";

var SELECTED_STYLE =
  "position:absolute;pointer-events:none;box-sizing:border-box;display:none;z-index:2;" +
  "border:2px solid #c15f3c;background:rgba(193,95,60,0.16);";

function consume(event) {
  event.preventDefault();
  event.stopImmediatePropagation();
}

/** True when the event's dispatch path crosses our overlay chrome. */
function overlayPath(event) {
  var path = event.composedPath ? event.composedPath() : [];
  for (var i = 0; i < path.length; i++) {
    if (path[i] && path[i].id === OVERLAY_HOST_ID) return true;
  }
  return isOverlayNode(event.target);
}

/** Same walk as `overlayPath`, for a node instead of an event. */
function isOverlayNode(node) {
  for (var current = node; current; current = current.parentNode || current.host) {
    if (current.nodeType === 1 && current.id === OVERLAY_HOST_ID) return true;
  }
  return false;
}

/**
 * Deep hit test through open shadow roots (no iframe recursion), with the
 * freeze's shield opened and our overlay's pointer-events turned off so the
 * page under both is what answers.
 */
function deepElementFromPoint(x, y) {
  var host = document.getElementById(OVERLAY_HOST_ID);
  var previous = host ? host.style.pointerEvents : null;
  ns.freeze.openForHitTest();
  if (host) host.style.pointerEvents = "none";
  var element;
  try {
    element = document.elementFromPoint(x, y);
    while (element) {
      var root = element.shadowRoot;
      var inner =
        root && typeof root.elementFromPoint === "function" ? root.elementFromPoint(x, y) : null;
      if (inner && inner !== element) {
        element = inner;
        continue;
      }
      break;
    }
  } finally {
    if (host) host.style.pointerEvents = previous || "none";
    ns.freeze.closeAfterHitTest();
  }
  if (!element || element === document.documentElement || element === document.body) return null;
  return isOverlayNode(element) ? null : element;
}

/** The composed parent, crossing open shadow boundaries (`assignedSlot` first). */
function composedParent(element) {
  if (element.assignedSlot) return element.assignedSlot;
  if (element.parentElement) return element.parentElement;
  var rootNode = element.getRootNode ? element.getRootNode() : null;
  if (rootNode && rootNode.host) return rootNode.host;
  return null;
}

/** Document coordinates, so the outline follows scrolling without listeners. */
function pageRect(element) {
  var rect = element.getBoundingClientRect();
  var sx = window.scrollX || window.pageXOffset || 0;
  var sy = window.scrollY || window.pageYOffset || 0;
  return { x: rect.left + sx, y: rect.top + sy, w: rect.width, h: rect.height };
}

function showOutline(element) {
  var rect = pageRect(element);
  outline.style.display = "block";
  outline.style.left = rect.x + "px";
  outline.style.top = rect.y + "px";
  outline.style.width = rect.w + "px";
  outline.style.height = rect.h + "px";
  label.textContent = describeElement(element, rect);
  label.style.display = "block";
  label.style.left = rect.x + "px";
  label.style.top = Math.max(window.scrollY || 0, rect.y - 20) + "px";
}

function describeElement(element, rect) {
  var name = element.tagName.toLowerCase();
  if (element.classList.length > 0) name += "." + element.classList[0];
  return name + "  " + Math.round(rect.w) + "×" + Math.round(rect.h);
}

function hideOutline() {
  hoverEl = null;
  if (outline) outline.style.display = "none";
  if (label) label.style.display = "none";
}

/** The dashed region box in page coordinates, so it survives scrolling. */
function showRegion(start, end) {
  if (!regionBox) return;
  var point = pagePoint();
  regionBox.style.display = "block";
  regionBox.style.left = Math.min(start.x, end.x) + point.sx + "px";
  regionBox.style.top = Math.min(start.y, end.y) + point.sy + "px";
  regionBox.style.width = Math.abs(end.x - start.x) + "px";
  regionBox.style.height = Math.abs(end.y - start.y) + "px";
}

function hideRegion() {
  if (regionBox) regionBox.style.display = "none";
}

/** The picked rect in page coordinates, so it follows scrolling like hover. */
function showSelected(rect) {
  if (!selected) return;
  selected.style.display = "block";
  selected.style.left = rect.x + "px";
  selected.style.top = rect.y + "px";
  selected.style.width = rect.w + "px";
  selected.style.height = rect.h + "px";
}

function clearSelected() {
  pendingPick = false;
  if (selected) selected.style.display = "none";
}

function crossedSlop(start, end) {
  var dx = end.x - start.x;
  var dy = end.y - start.y;
  return dx * dx + dy * dy >= CLICK_SLOP * CLICK_SLOP;
}

function onPointerMove(event) {
  if (overlayPath(event)) return;
  consume(event);
  if (!isTrustedAction(event)) return;
  lastPoint = { x: event.clientX, y: event.clientY };
  if (pendingPick) return;
  if (downPoint) {
    if (dragging || crossedSlop(downPoint, lastPoint)) {
      dragging = true;
      showRegion(downPoint, lastPoint);
      if (outline) outline.style.display = "none";
      if (label) label.style.display = "none";
      return;
    }
  }
  var element = deepElementFromPoint(event.clientX, event.clientY);
  if (element === hoverEl) return;
  hoverEl = element;
  if (element) showOutline(element);
  else hideOutline();
}

function onPointerDown(event) {
  if (overlayPath(event)) return;
  consume(event);
  if (!isTrustedAction(event)) return;
  lastPoint = { x: event.clientX, y: event.clientY };
  if (pendingPick) return;
  downPoint = { x: event.clientX, y: event.clientY };
}

function onPointerUp(event) {
  if (overlayPath(event)) return;
  consume(event);
  if (!isTrustedAction(event)) return;
  lastPoint = { x: event.clientX, y: event.clientY };
  var point = downPoint;
  downPoint = null;
  if (!point || pendingPick) {
    dragging = false;
    hideRegion();
    return;
  }
  if (dragging || crossedSlop(point, lastPoint)) {
    dragging = false;
    hideRegion();
    pickRegion(point, lastPoint);
    return;
  }
  var element = deepElementFromPoint(event.clientX, event.clientY);
  if (element) pick(element);
}

function onConsume(event) {
  if (overlayPath(event)) return;
  consume(event);
}

function onKeyDown(event) {
  // Escape leaves the mode, with or without a pending pick: the parent drops
  // its composer on `modeChanged {on:false}`. Synthetic events are swallowed
  // but never act (page scripts share the realm).
  if (event.isComposing) return;
  if (event.key !== "Escape") return;
  consume(event);
  if (isTrustedAction(event)) applyMode(false, "escape");
}

function pagePoint() {
  return {
    sx: window.scrollX || window.pageXOffset || 0,
    sy: window.scrollY || window.pageYOffset || 0,
  };
}

/** Capture crop rect: PICK_MARGIN around the element, clipped to the document. */
function marginRect(rect) {
  var doc = document.documentElement;
  var docW = Math.max(doc.scrollWidth, document.body ? document.body.scrollWidth : 0);
  var docH = Math.max(doc.scrollHeight, document.body ? document.body.scrollHeight : 0);
  var x1 = Math.max(0, rect.x - PICK_MARGIN);
  var y1 = Math.max(0, rect.y - PICK_MARGIN);
  var x2 = rect.x + rect.w + PICK_MARGIN;
  var y2 = rect.y + rect.h + PICK_MARGIN;
  if (docW > 0) x2 = Math.min(x2, docW);
  if (docH > 0) y2 = Math.min(y2, docH);
  x2 = Math.max(x1, x2);
  y2 = Math.max(y1, y2);
  return { x: x1, y: y1, w: x2 - x1, h: y2 - y1 };
}

function buildAnchor(element, rect, kind, region, text) {
  var diagnostics = ns.diagnostics ? ns.diagnostics.snapshot() : null;
  return {
    v: 1,
    kind: kind || "element",
    page: {
      url: location.origin + location.pathname,
      title: document.title || "",
      vw: window.innerWidth || 0,
      vh: window.innerHeight || 0,
      sx: window.scrollX || window.pageXOffset || 0,
      sy: window.scrollY || window.pageYOffset || 0,
      dpr: window.devicePixelRatio || 1,
    },
    target: ns.generateTarget(element),
    rect: rect,
    region: region || null,
    selectedText: text || "",
    console: diagnostics && Array.isArray(diagnostics.console) ? diagnostics.console : [],
    network: diagnostics && Array.isArray(diagnostics.network) ? diagnostics.network : [],
    screenshot: null,
  };
}

/** The visible text of the text nodes whose client rects intersect the box. */
function collectSelectedText(clientBox) {
  var root = document.body || document.documentElement;
  if (!root || typeof document.createTreeWalker !== "function") return "";
  var walker = document.createTreeWalker(root, 4 /* SHOW_TEXT */, null);
  var parts = [];
  var node = walker.nextNode();
  while (node) {
    var text = node.nodeValue;
    if (text && /\S/.test(text)) {
      var range = document.createRange();
      range.selectNodeContents(node);
      var rects = typeof range.getClientRects === "function" ? range.getClientRects() : [];
      for (var i = 0; i < rects.length; i++) {
        var rect = rects[i];
        if (
          rect.left < clientBox.right &&
          rect.right > clientBox.left &&
          rect.top < clientBox.bottom &&
          rect.bottom > clientBox.top
        ) {
          parts.push(text);
          break;
        }
      }
    }
    if (parts.join(" ").length > 1000) break;
    node = walker.nextNode();
  }
  return parts.join(" ").replace(/\s+/g, " ").trim().slice(0, 500);
}

/** The smallest composed ancestor whose rect covers the region box. */
function coveringElement(centerX, centerY, region) {
  var current = deepElementFromPoint(centerX, centerY) || document.body;
  while (current && current !== document.body) {
    var rect = pageRect(current);
    if (
      rect.x <= region.x &&
      rect.y <= region.y &&
      rect.x + rect.w >= region.x + region.w &&
      rect.y + rect.h >= region.y + region.h
    ) {
      return current;
    }
    current = composedParent(current);
  }
  return document.body;
}

function pickRegion(start, end) {
  hideOutline();
  var point = pagePoint();
  var clientBox = {
    left: Math.min(start.x, end.x),
    top: Math.min(start.y, end.y),
    right: Math.max(start.x, end.x),
    bottom: Math.max(start.y, end.y),
  };
  var region = {
    x: clientBox.left + point.sx,
    y: clientBox.top + point.sy,
    w: clientBox.right - clientBox.left,
    h: clientBox.bottom - clientBox.top,
  };
  var target = coveringElement(
    (clientBox.left + clientBox.right) / 2,
    (clientBox.top + clientBox.bottom) / 2,
    region,
  );
  var rect = pageRect(target);
  var anchor = buildAnchor(target, rect, "region", region, collectSelectedText(clientBox));
  openPickerCapture(anchor, region, region, {
    x: clientBox.left,
    y: clientBox.top,
    w: clientBox.right - clientBox.left,
    h: clientBox.bottom - clientBox.top,
  });
}

/** Capture the crop, then post the pick (falling back without a screenshot). */
function openPickerCapture(anchor, outlineRect, captureRect, viewportRect) {
  if (capturePending || pendingPick) return;
  if (typeof ns.capture !== "function") {
    finishPick(anchor, outlineRect, null, viewportRect);
    return;
  }
  capturePending = true;
  var generation = modeGeneration;
  Promise.resolve()
    .then(function () {
      return ns.capture(captureRect);
    })
    .then(
      function (screenshot) {
        if (generation !== modeGeneration) return;
        capturePending = false;
        if (on && !pendingPick) finishPick(anchor, outlineRect, screenshot || null, viewportRect);
      },
      function () {
        if (generation !== modeGeneration) return;
        capturePending = false;
        if (on && !pendingPick) finishPick(anchor, outlineRect, null, viewportRect);
      },
    );
}

/** Keep the outline of the picked rect and report it; no note leaves the frame. */
function finishPick(anchor, outlineRect, screenshot, viewportRect) {
  pendingPick = true;
  showSelected(outlineRect);
  ns.send({
    type: "annotate:picked",
    anchor: anchor,
    screenshot: screenshot,
    viewportRect: viewportRect,
  });
}

function pick(element) {
  hideOutline();
  var rect = pageRect(element);
  var point = pagePoint();
  openPickerCapture(buildAnchor(element, rect), rect, marginRect(rect), {
    x: rect.x - point.sx,
    y: rect.y - point.sy,
    w: rect.w,
    h: rect.h,
  });
}

function installUi() {
  container = document.createElement("div");
  container.setAttribute("data-omni-pick", "layer");
  container.style.cssText = "position:absolute;top:0;left:0;pointer-events:none;";
  outline = document.createElement("div");
  outline.setAttribute("data-omni-pick", "outline");
  outline.style.cssText = OUTLINE_STYLE;
  label = document.createElement("div");
  label.setAttribute("data-omni-pick", "hover-label");
  label.style.cssText = HOVER_LABEL_STYLE;
  regionBox = document.createElement("div");
  regionBox.setAttribute("data-omni-pick", "region");
  regionBox.style.cssText = REGION_STYLE;
  selected = document.createElement("div");
  selected.setAttribute("data-omni-pick", "selected");
  selected.style.cssText = SELECTED_STYLE;
  container.appendChild(outline);
  container.appendChild(label);
  container.appendChild(regionBox);
  container.appendChild(selected);
  ns.overlayRoot().appendChild(container);

  cursorStyle = document.createElement("style");
  cursorStyle.id = "__omni-annotate-cursor";
  cursorStyle.textContent = "*{cursor:crosshair !important;}";
  (document.head || document.documentElement).appendChild(cursorStyle);
}

function removeUi() {
  if (container && container.parentNode) container.parentNode.removeChild(container);
  container = null;
  outline = null;
  label = null;
  regionBox = null;
  selected = null;
  if (cursorStyle && cursorStyle.parentNode) cursorStyle.parentNode.removeChild(cursorStyle);
  cursorStyle = null;
  hoverEl = null;
}

/** The head capture script's early slot registry, when it is present. */
function captureSlotRegistry() {
  var capture = window["__omniCapture"];
  return capture && typeof capture.setSlot === "function" ? capture : null;
}

function addListener(type, handler) {
  var capture = captureSlotRegistry();
  if (capture) {
    capture.setSlot(type, handler);
    listeners.push([type, handler, true]);
    return;
  }
  window.addEventListener(type, handler, true);
  listeners.push([type, handler, false]);
}

function enterMode() {
  window.addEventListener("pagehide", onPageHide);
  ns.freeze.on(lastPoint ? lastPoint.x : undefined, lastPoint ? lastPoint.y : undefined);
  installUi();
  // With the capture script present, the stub owns the composed keydown slot
  // and calls ns.pickerKeydown after its shortcut match.
  if (captureSlotRegistry()) ns.pickerKeydown = onKeyDown;
  else addListener("keydown", onKeyDown);
  addListener("pointermove", onPointerMove);
  addListener("pointerdown", onPointerDown);
  addListener("pointerup", onPointerUp);
  addListener("click", onConsume);
  addListener("mousedown", onConsume);
  addListener("mouseup", onConsume);
  addListener("contextmenu", onConsume);
  addListener("dblclick", onConsume);
}

function teardown() {
  window.removeEventListener("pagehide", onPageHide);
  for (var i = 0; i < listeners.length; i++) {
    if (listeners[i][2]) window["__omniCapture"].setSlot(listeners[i][0], null);
    else window.removeEventListener(listeners[i][0], listeners[i][1], true);
  }
  listeners = [];
  ns.pickerKeydown = null;
  pendingPick = false;
  removeUi();
  downPoint = null;
  dragging = false;
  lastPoint = null;
  capturePending = false;
  modeGeneration++;
  // A capture still in flight must not keep our chrome hidden or come back to
  // hide the next mode's chrome.
  if (typeof ns.captureRelease === "function") ns.captureRelease();
  ns.freeze.off();
}

/** Leaving the document is a full mode exit: no freeze or chrome may outlive it. */
function onPageHide() {
  if (on) applyMode(false);
}

function applyMode(next, reason) {
  if (next === on) return;
  on = next;
  if (on) enterMode();
  else teardown();
  if (reason) ns.send({ type: "annotate:modeChanged", on: on, reason: reason });
}

/**
 * Toggle from inside the frame (the stub's shortcut slot). Only the parent
 * turns the mode on, so while off this asks it to; while on the frame leaves
 * locally and reports the change.
 */
ns.toggleMode = function (reason) {
  if (!on) {
    ns.send({ type: "annotate:toggleRequested" });
    return;
  }
  applyMode(false, reason || "shortcut");
};

ns.on("annotate:setMode", function (msg) {
  applyMode(!!msg.on);
});

// The parent drops a pick on cancel, on submit, or when the mode turns off.
ns.on("annotate:pickDone", function () {
  clearSelected();
});
