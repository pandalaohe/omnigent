// Numbered marker overlay for element annotations: paints one badge per
// resolved anchor in a single closed-shadow host, re-resolves orphaned or
// replaced nodes on DOM mutations, and reports changes over the annotate port.
// Provides ns.overlayRoot() so later runtime parts can append their own UI.

var OVERLAY_HOST_ID = "__omni-annotate-host";
var DEBOUNCE_MS = 250;
var BADGE_STYLE =
  "position:absolute;pointer-events:auto;box-sizing:border-box;min-width:18px;height:18px;" +
  "padding:0 4px;border-radius:9px;border:1px solid #fff;background:#e11d48;color:#fff;" +
  "font:600 11px/16px system-ui,sans-serif;text-align:center;cursor:pointer;" +
  "box-shadow:0 1px 3px rgba(0,0,0,0.4);";

var host = null;
var shadow = null;
var layer = null;
var markers = []; // { id, n, anchor, element, badge }
var observer = null;
var debounceTimer = null;
var tracking = false;
var scrollQueued = false;

/** The overlay's closed shadow root; the host is created on first use. */
function overlayRoot() {
  if (shadow) return shadow;
  host = document.createElement("div");
  host.id = OVERLAY_HOST_ID;
  host.style.cssText =
    "position:absolute;top:0;left:0;width:0;height:0;pointer-events:none;z-index:2147483647;";
  shadow = host.attachShadow({ mode: "closed" });
  layer = document.createElement("div");
  layer.style.cssText = "position:absolute;top:0;left:0;pointer-events:none;";
  shadow.appendChild(layer);
  (document.documentElement || document.body).appendChild(host);
  return shadow;
}

/** Viewport rect plus the current scroll: a badge re-read this way follows its
 * content, and a fixed/sticky target stays visually attached while scrolling. */
function position(marker) {
  if (!marker.element || !marker.badge) return;
  var rect = marker.element.getBoundingClientRect();
  var left = rect.left + (window.scrollX || window.pageXOffset || 0);
  var top = rect.top + (window.scrollY || window.pageYOffset || 0);
  marker.badge.style.left = left + "px";
  marker.badge.style.top = top + "px";
}

function repositionAll() {
  for (var i = 0; i < markers.length; i++) position(markers[i]);
}

function onScroll() {
  if (scrollQueued) return;
  scrollQueued = true;
  requestAnimationFrame(function () {
    scrollQueued = false;
    repositionAll();
  });
}

function onResize() {
  repositionAll();
}

/** Capture phase: a nested container's scroll never bubbles to window. */
function startTracking() {
  if (tracking) return;
  tracking = true;
  window.addEventListener("scroll", onScroll, true);
  window.addEventListener("resize", onResize);
}

function stopTracking() {
  if (!tracking) return;
  tracking = false;
  window.removeEventListener("scroll", onScroll, true);
  window.removeEventListener("resize", onResize);
}

function makeBadge(marker) {
  var badge = document.createElement("button");
  badge.type = "button";
  badge.className = "omni-annotate-marker";
  badge.textContent = String(marker.n);
  badge.setAttribute("aria-label", "Annotation " + marker.n);
  badge.style.cssText = BADGE_STYLE;
  badge.addEventListener("click", function (event) {
    event.preventDefault();
    event.stopPropagation();
    ns.send({ type: "annotate:markerClick", id: marker.id });
  });
  layer.appendChild(badge);
  return badge;
}

function safeResolve(target) {
  try {
    return ns.resolveTarget(target) || null;
  } catch {
    return null;
  }
}

function insideOverlay(node) {
  for (var current = node; current; current = current.parentNode || current.host) {
    if (current === host) return true;
  }
  return false;
}

function scheduleRecheck() {
  if (debounceTimer) clearTimeout(debounceTimer);
  debounceTimer = setTimeout(recheck, DEBOUNCE_MS);
}

function observe() {
  if (observer || !document.body) return;
  observer = new MutationObserver(function (mutations) {
    for (var i = 0; i < mutations.length; i++) {
      if (!insideOverlay(mutations[i].target)) {
        scheduleRecheck();
        return;
      }
    }
  });
  observer.observe(document.body, {
    subtree: true,
    childList: true,
    attributes: true,
    characterData: true,
  });
}

/** Re-resolve every marker and reposition every attached badge; on any `found`
 * flip, report the full marker state once per batch. */
function recheck() {
  debounceTimer = null;
  var flipped = false;
  for (var i = 0; i < markers.length; i++) {
    var marker = markers[i];
    var wasFound = marker.element !== null;
    var next = safeResolve(marker.anchor.target);
    if (next !== marker.element) {
      if (marker.badge) {
        marker.badge.remove();
        marker.badge = null;
      }
      marker.element = next;
      if (next) marker.badge = makeBadge(marker);
    }
    position(marker);
    if (wasFound !== (marker.element !== null)) flipped = true;
  }
  if (!flipped) return;
  var state = [];
  for (var j = 0; j < markers.length; j++) {
    state.push({ id: markers[j].id, found: markers[j].element !== null });
  }
  ns.send({ type: "annotate:resolved", items: state });
}

function onSetAnnotations(msg) {
  overlayRoot();
  for (var i = 0; i < markers.length; i++) {
    if (markers[i].badge) markers[i].badge.remove();
  }
  markers = [];

  var items = Array.isArray(msg.items) ? msg.items : [];
  var resolved = [];
  for (var j = 0; j < items.length; j++) {
    var item = items[j] || {};
    var anchor = item.anchor || {};
    var element = safeResolve(anchor.target);
    var marker = { id: item.id, n: item.n, anchor: anchor, element: element, badge: null };
    if (element) {
      marker.badge = makeBadge(marker);
      position(marker);
    }
    markers.push(marker);
    resolved.push({ id: item.id, found: element !== null });
  }

  observe();
  if (markers.length > 0) startTracking();
  else stopTracking();
  ns.send({ type: "annotate:resolved", items: resolved });
}

ns.overlayRoot = overlayRoot;
ns.on("annotate:setAnnotations", onSetAnnotations);
