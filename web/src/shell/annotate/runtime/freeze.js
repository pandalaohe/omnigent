// In-frame freeze runtime (design §2.9): pins the :hover/:focus computed styles
// (display and visibility included) onto inline styles, blocks the enter/leave/
// over/out and focus/blur side effects in the capture phase, pauses running
// animations (WAAPI, CSS sheet, SVG) and holds page requestAnimationFrame
// callbacks, then installs the pointer-events layer and the hit-test shield that
// keep the page from reacting while the picker is active. Everything reverses
// on `off()`.
//
// A port of react-grab (MIT, commit ea4bbec9): freeze-global-interactions.ts,
// freeze-pseudo-states.ts, freeze-animations.ts, freeze-animation-frame-loops.ts,
// native-raf.ts, pointer-events-freeze.ts, create-hit-test-shield.ts,
// create-style-element.ts, find-scrollable-ancestor.ts,
// get-composed-parent-element.ts, get-deep-hovered-elements.ts and
// throw-collected-errors.ts, with the React renderer freeze, iframe recursion
// and the element position cache dropped (survey R2). The licence text is in
// THIRD_PARTY_NOTICES.md.

var OVERLAY_HOST_ID = "__omni-annotate-host";
var SHIELD_ID = "__omni-annotate-shield";
var SVG_NS = "http://www.w3.org/2000/svg";

// react-grab constants, inlined (constants.ts).
var Z_INDEX_HIT_TEST_SHIELD = 2147483644;
var WAAPI_GLOBAL_FREEZE_MAX_ANIMATIONS = 200;
var WHEEL_LINE_DELTA_PX = 16;
var SCROLL_ROOM_EPSILON_PX = 1;
var MAX_HOVER_DESCENDANT_ELEMENTS = 2000;

var MOUSE_EVENTS_TO_BLOCK = [
  "mouseenter",
  "mouseleave",
  "mouseover",
  "mouseout",
  "pointerenter",
  "pointerleave",
  "pointerover",
  "pointerout",
];
var FOCUS_EVENTS_TO_BLOCK = ["focus", "blur", "focusin", "focusout"];

var HOVER_STYLE_PROPERTIES = [
  "background-color",
  "color",
  "border-color",
  "box-shadow",
  "transform",
  "opacity",
  "outline",
  "filter",
  "scale",
  "visibility",
  "display",
];
var FOCUS_STYLE_PROPERTIES = [
  "background-color",
  "color",
  "border-color",
  "box-shadow",
  "outline",
  "outline-offset",
  "outline-width",
  "outline-color",
  "outline-style",
  "filter",
  "opacity",
  "ring-color",
  "ring-width",
];
// A hovered ancestor can reveal a descendant (`:hover .menu { display:block }`);
// the descendant needs its own pin or the shield hides it.
var HOVER_DESCENDANT_STYLE_PROPERTIES = ["display", "visibility", "opacity"];

var GLOBAL_FREEZE_STYLES =
  "\n*, *::before, *::after {\n" +
  "  animation-play-state: paused !important;\n" +
  "  transition: none !important;\n" +
  "}\n";

var frozenHoverElements = new Map();
var frozenFocusElements = new Map();
var isFreezeApplied = false;

var pointerEventsStyle = null;
var shieldContainer = null;
var shieldPanel = null;

var animationStyleElement = null;
var frozenSvgElements = [];
var frozenWaapiAnimations = [];
var hasGlobalAnimationFreeze = false;

// ---------------------------------------------------------------------------
// Element helpers
// ---------------------------------------------------------------------------

function isElementNode(node) {
  return typeof node === "object" && node !== null && node.nodeType === 1;
}

function isHtmlElement(element) {
  return isElementNode(element) && element.namespaceURI === "http://www.w3.org/1999/xhtml";
}

/** Our own chrome (overlay host and hit-test shield) and everything inside it. */
function isOurChrome(node) {
  for (var current = node; current; current = current.parentNode || current.host) {
    if (current.nodeType === 1 && (current.id === OVERLAY_HOST_ID || current.id === SHIELD_ID)) {
      return true;
    }
  }
  return false;
}

function getComposedParentElement(element) {
  if (element.assignedSlot) return element.assignedSlot;
  if (element.parentElement) return element.parentElement;
  var rootNode = element.getRootNode ? element.getRootNode() : null;
  if (rootNode && rootNode.host) return rootNode.host;
  return null;
}

/** Deep hit test through open shadow roots; iframes are never entered. */
function deepElementFromPoint(x, y) {
  var element = document.elementFromPoint(x, y);
  while (element && element.shadowRoot) {
    var root = element.shadowRoot;
    var inner = typeof root.elementFromPoint === "function" ? root.elementFromPoint(x, y) : null;
    if (!inner || inner === element) break;
    element = inner;
  }
  return element || null;
}

// ---------------------------------------------------------------------------
// Pseudo-state pinning
// ---------------------------------------------------------------------------

function freezeElement(element, properties) {
  var computed = getComputedStyle(element);
  var frozenPropertyValues = new Map();
  var originalPropertyValues = new Map();
  for (var i = 0; i < properties.length; i++) {
    var property = properties[i];
    var computedValue = computed.getPropertyValue(property);
    if (!computedValue) continue;
    frozenPropertyValues.set(property, computedValue);
    originalPropertyValues.set(property, {
      value: element.style.getPropertyValue(property),
      priority: element.style.getPropertyPriority(property),
    });
  }
  return {
    element: element,
    frozenPropertyValues: frozenPropertyValues,
    originalPropertyValues: originalPropertyValues,
  };
}

function applyFrozenStates(states, storageMap) {
  for (var i = 0; i < states.length; i++) {
    var state = states[i];
    if (state.frozenPropertyValues.size === 0) continue;
    storageMap.set(state.element, state);
    var entries = Array.from(state.frozenPropertyValues);
    for (var j = 0; j < entries.length; j++) {
      state.element.style.setProperty(entries[j][0], entries[j][1], "important");
    }
  }
}

// Restore only the declarations this freeze still owns: a page write made while
// frozen has replaced the frozen value and must survive `off`.
function restoreFrozenStates(storageMap, cleanupErrors) {
  storageMap.forEach(function (state, element) {
    state.frozenPropertyValues.forEach(function (frozenValue, property) {
      try {
        if (
          element.style.getPropertyValue(property) !== frozenValue ||
          element.style.getPropertyPriority(property) !== "important"
        ) {
          return;
        }
        var originalProperty = state.originalPropertyValues.get(property);
        if (originalProperty.value) {
          element.style.setProperty(property, originalProperty.value, originalProperty.priority);
        } else {
          element.style.removeProperty(property);
        }
      } catch (error) {
        cleanupErrors.push(error);
      }
    });
  });
  storageMap.clear();
}

function collectHoveredInRoot(root, collected) {
  var hovered = root.querySelectorAll(":hover");
  for (var i = 0; i < hovered.length; i++) {
    var element = hovered[i];
    if (isOurChrome(element)) continue;
    if (isHtmlElement(element)) collected.push(element);
    if (element.shadowRoot) collectHoveredInRoot(element.shadowRoot, collected);
  }
}

function getDeepHoveredElements() {
  var collected = [];
  collectHoveredInRoot(document, collected);
  return collected;
}

function hoveredElementsAtPoint(x, y) {
  var collected = [];
  var current = deepElementFromPoint(x, y);
  while (current && current !== document.documentElement) {
    if (isOurChrome(current)) break;
    if (isHtmlElement(current)) collected.push(current);
    current = getComposedParentElement(current);
  }
  return collected;
}

function collectFocusedElements() {
  var collected = [];
  var current = document.activeElement;
  while (current && current !== document.body) {
    if (isOurChrome(current)) break;
    if (isHtmlElement(current)) collected.push(current);
    var inner = current.shadowRoot ? current.shadowRoot.activeElement : null;
    if (inner) {
      current = inner;
      continue;
    }
    current = null;
  }
  return collected;
}

// Hover-revealed descendants of any hovered element keep their computed
// display/visibility/opacity; the budget bounds a whole-document walk.
function collectHoverDescendantStates(hoveredElements) {
  var descendantStates = [];
  var visited = new Set();
  for (var i = 0; i < hoveredElements.length; i++) visited.add(hoveredElements[i]);
  var queue = hoveredElements.slice();
  var budget = MAX_HOVER_DESCENDANT_ELEMENTS;
  while (queue.length > 0 && budget > 0) {
    var children = queue.shift().children;
    for (var j = 0; j < children.length && budget > 0; j++) {
      var child = children[j];
      budget--;
      if (visited.has(child)) continue;
      visited.add(child);
      queue.push(child);
      if (getComputedStyle(child).getPropertyValue("display") === "none") continue;
      descendantStates.push(freezeElement(child, HOVER_DESCENDANT_STYLE_PROPERTIES));
    }
  }
  return descendantStates;
}

// Before disabling pointer-events we snapshot the current :hover/:focus computed
// values onto inline styles so hover-revealed menus and focused controls keep
// their visual state once the pseudo classes stop matching.
function collectPseudoStates(cursorX, cursorY) {
  if (isFreezeApplied) return null;

  var isCursorInViewport =
    typeof cursorX === "number" &&
    typeof cursorY === "number" &&
    cursorX >= 0 &&
    cursorY >= 0 &&
    cursorX < window.innerWidth &&
    cursorY < window.innerHeight;
  var hoveredElements = isCursorInViewport
    ? hoveredElementsAtPoint(cursorX, cursorY)
    : getDeepHoveredElements();

  var hoverStates = [];
  for (var i = 0; i < hoveredElements.length; i++) {
    var hoverState = freezeElement(hoveredElements[i], HOVER_STYLE_PROPERTIES);
    if (hoverState) hoverStates.push(hoverState);
  }
  hoverStates = hoverStates.concat(collectHoverDescendantStates(hoveredElements));

  var focusStates = [];
  var focusedElements = collectFocusedElements();
  for (var j = 0; j < focusedElements.length; j++) {
    var focusState = freezeElement(focusedElements[j], FOCUS_STYLE_PROPERTIES);
    if (focusState) focusStates.push(focusState);
  }

  return { hoverStates: hoverStates, focusStates: focusStates };
}

// Capture-phase blockers prevent hover and focus side effects while the
// pointer-events layer is briefly suspended for hit-testing.
function stopEvent(event) {
  event.stopImmediatePropagation();
}

function preventFocusChange(event) {
  event.preventDefault();
  event.stopImmediatePropagation();
}

/** The head capture script's early slot registry, when it is present. */
function captureSlotRegistry() {
  var capture = window["__omniCapture"];
  return capture && typeof capture.setSlot === "function" ? capture : null;
}

// Event blockers installed through the head script's early slots; without the
// registry they fall back to capture-phase listeners on `document`.
var slotBlockers = []; // [type, handler, viaCaptureSlot]

function addEventBlocker(type, handler) {
  var capture = captureSlotRegistry();
  if (capture) {
    capture.setSlot(type, handler);
    slotBlockers.push([type, handler, true]);
    return;
  }
  document.addEventListener(type, handler, true);
  slotBlockers.push([type, handler, false]);
}

function removeEventBlockers() {
  var capture = captureSlotRegistry();
  for (var i = 0; i < slotBlockers.length; i++) {
    if (slotBlockers[i][2]) {
      if (capture) capture.setSlot(slotBlockers[i][0], null);
    } else {
      document.removeEventListener(slotBlockers[i][0], slotBlockers[i][1], true);
    }
  }
  slotBlockers = [];
}

// The early slots run before page listeners registered before mode entry, so a
// page hover or focus handler cannot close a menu that is open when the mode
// starts.
function addEventBlockers() {
  for (var i = 0; i < MOUSE_EVENTS_TO_BLOCK.length; i++) {
    addEventBlocker(MOUSE_EVENTS_TO_BLOCK[i], stopEvent);
  }
  for (var j = 0; j < FOCUS_EVENTS_TO_BLOCK.length; j++) {
    addEventBlocker(FOCUS_EVENTS_TO_BLOCK[j], preventFocusChange);
  }
}

function applyPseudoStates(snapshot) {
  if (!snapshot) return;
  isFreezeApplied = true;
  addEventBlockers();
  applyFrozenStates(snapshot.hoverStates, frozenHoverElements);
  applyFrozenStates(snapshot.focusStates, frozenFocusElements);
  installPointerEventsFreeze();
}

function unfreezePseudoStates(cleanupErrors) {
  isFreezeApplied = false;
  removeEventBlockers();
  restoreFrozenStates(frozenHoverElements, cleanupErrors);
  restoreFrozenStates(frozenFocusElements, cleanupErrors);
  uninstallPointerEventsFreeze(cleanupErrors);
}

// ---------------------------------------------------------------------------
// Pointer-events layer and hit-test shield
// ---------------------------------------------------------------------------

function createStyleElement(attribute, content) {
  var element = document.createElement("style");
  element.setAttribute(attribute, "");
  element.textContent = content;
  (document.head || document.documentElement).appendChild(element);
  return element;
}

function onShieldWheel(event) {
  var deltaX = event.deltaX;
  var deltaY = event.deltaY;
  if (event.deltaMode === 1) {
    deltaX *= WHEEL_LINE_DELTA_PX;
    deltaY *= WHEEL_LINE_DELTA_PX;
  } else if (event.deltaMode === 2) {
    deltaX *= document.documentElement.clientWidth;
    deltaY *= document.documentElement.clientHeight;
  }
  if (deltaX === 0 && deltaY === 0) return;

  openForHitTest();
  var element;
  try {
    element = deepElementFromPoint(event.clientX, event.clientY);
  } finally {
    closeAfterHitTest();
  }
  if (!element) return;

  var scrollTarget = findScrollableAncestor(element, deltaX, deltaY);
  if (!scrollTarget) return;

  event.preventDefault();
  scrollTarget.scrollBy({ left: deltaX, top: deltaY, behavior: "instant" });
}

/** A shield above the page absorbs hover, focus and click; the panels turn
 * pointer-events off only for the synchronous hit test that needs to see the
 * page underneath. `html, body { pointer-events: auto !important }` is the
 * opposite half: a modal layer setting `body { pointer-events: none }` must not
 * blind the hit test. */
function createShield() {
  var container = document.createElement("div");
  container.id = SHIELD_ID;
  container.setAttribute("aria-hidden", "true");
  container.style.cssText =
    "position:fixed;inset:0;pointer-events:none;contain:strict;background:transparent;" +
    "z-index:" +
    Z_INDEX_HIT_TEST_SHIELD +
    ";";
  var panel = document.createElement("div");
  panel.style.position = "absolute";
  panel.style.inset = "0";
  panel.style.pointerEvents = "auto";
  panel.style.background = "transparent";
  container.appendChild(panel);
  container.addEventListener("wheel", onShieldWheel, { passive: false });
  (document.body || document.documentElement).appendChild(container);
  shieldContainer = container;
  shieldPanel = panel;
}

var hitTestDepth = 0;

function openForHitTest() {
  hitTestDepth++;
  if (shieldPanel) shieldPanel.style.pointerEvents = "none";
}

function closeAfterHitTest() {
  if (hitTestDepth > 0) hitTestDepth--;
  if (hitTestDepth === 0 && shieldPanel) shieldPanel.style.pointerEvents = "auto";
}

function installPointerEventsFreeze() {
  if (pointerEventsStyle) return;
  hitTestDepth = 0;
  pointerEventsStyle = createStyleElement(
    "data-omni-frozen-pointer-events",
    "html, body { pointer-events: auto !important; }",
  );
  createShield();
}

function uninstallPointerEventsFreeze(cleanupErrors) {
  if (pointerEventsStyle) {
    try {
      pointerEventsStyle.remove();
    } catch (error) {
      cleanupErrors.push(error);
    }
    pointerEventsStyle = null;
  }
  if (shieldContainer) {
    try {
      shieldContainer.removeEventListener("wheel", onShieldWheel);
      shieldContainer.remove();
    } catch (error) {
      cleanupErrors.push(error);
    }
    shieldContainer = null;
    shieldPanel = null;
  }
}

var SCROLLABLE_OVERFLOW_VALUES = ["auto", "scroll", "overlay"];

function canScrollAxis(overflow, scrollPosition, clientSize, scrollSize, delta) {
  if (SCROLLABLE_OVERFLOW_VALUES.indexOf(overflow) === -1) return false;
  if (delta < 0) return scrollPosition > SCROLL_ROOM_EPSILON_PX;
  return scrollPosition + clientSize < scrollSize - SCROLL_ROOM_EPSILON_PX;
}

function findScrollableAncestor(element, deltaX, deltaY) {
  var current = element;
  while (current) {
    var hasVerticalOverflow = deltaY !== 0 && current.scrollHeight > current.clientHeight;
    var hasHorizontalOverflow = deltaX !== 0 && current.scrollWidth > current.clientWidth;
    if (hasVerticalOverflow || hasHorizontalOverflow) {
      var style = getComputedStyle(current);
      if (
        hasVerticalOverflow &&
        canScrollAxis(
          style.overflowY,
          current.scrollTop,
          current.clientHeight,
          current.scrollHeight,
          deltaY,
        )
      ) {
        return current;
      }
      if (
        hasHorizontalOverflow &&
        canScrollAxis(
          style.overflowX,
          current.scrollLeft,
          current.clientWidth,
          current.scrollWidth,
          deltaX,
        )
      ) {
        return current;
      }
    }
    current = getComposedParentElement(current);
  }
  return null;
}

// ---------------------------------------------------------------------------
// Animation pause and rAF hold
// ---------------------------------------------------------------------------

function isSvgRootElement(element) {
  return !!element && element.namespaceURI === SVG_NS && element.tagName === "svg";
}

function collectFrozenSvgElements(elements) {
  var svgElements = new Set();
  for (var i = 0; i < elements.length; i++) {
    var element = elements[i];
    var containing = isSvgRootElement(element) ? element : element.closest("svg");
    if (isSvgRootElement(containing)) svgElements.add(containing);
    var inner = element.querySelectorAll("svg");
    for (var j = 0; j < inner.length; j++) {
      if (isSvgRootElement(inner[j])) svgElements.add(inner[j]);
    }
  }
  return Array.from(svgElements);
}

function callSvgAnimationMethod(svgElement, methodName) {
  var method = Reflect.get(svgElement, methodName);
  if (typeof method !== "function") return;
  method.call(svgElement);
}

function pauseSvgAnimations(svgElements, storage) {
  for (var i = 0; i < svgElements.length; i++) {
    var svgElement = svgElements[i];
    if (typeof svgElement.animationsPaused === "function" && svgElement.animationsPaused()) {
      continue;
    }
    storage.push(svgElement);
    callSvgAnimationMethod(svgElement, "pauseAnimations");
  }
}

function resumeSvgAnimations(svgElements, cleanupErrors) {
  var stillFrozen = [];
  for (var i = 0; i < svgElements.length; i++) {
    try {
      callSvgAnimationMethod(svgElements[i], "unpauseAnimations");
    } catch (error) {
      stillFrozen.push(svgElements[i]);
      cleanupErrors.push(error);
    }
  }
  return stillFrozen;
}

function restorePausedAnimations(animations, cleanupErrors) {
  for (var i = 0; i < animations.length; i++) {
    try {
      animations[i].play();
    } catch (error) {
      cleanupErrors.push(error);
    }
  }
}

/** Animations targeting our own closed shadow root must keep running. */
function isShadowAnimation(animation) {
  if (typeof KeyframeEffect === "undefined" || !(animation.effect instanceof KeyframeEffect)) {
    return false;
  }
  var target = animation.effect.target;
  if (!isElementNode(target)) return false;
  var rootNode = target.getRootNode ? target.getRootNode() : null;
  return !!(rootNode && rootNode.host && isOurChrome(rootNode.host));
}

function collectDocumentAnimationsToFreeze() {
  var runningAnimations = [];
  if (typeof document.getAnimations !== "function") return runningAnimations;
  var animations = document.getAnimations();
  for (var i = 0; i < animations.length; i++) {
    var animation = animations[i];
    if (isShadowAnimation(animation)) continue;
    if (animation.playState === "running") runningAnimations.push(animation);
  }
  return runningAnimations;
}

function collectGlobalAnimationsToFreeze() {
  if (hasGlobalAnimationFreeze) return null;
  return collectDocumentAnimationsToFreeze();
}

function applyGlobalAnimationFreeze(runningAnimations) {
  if (runningAnimations === null || hasGlobalAnimationFreeze) return;
  hasGlobalAnimationFreeze = true;
  animationStyleElement = createStyleElement("data-omni-frozen-animations", "");
  if (runningAnimations.length > WAAPI_GLOBAL_FREEZE_MAX_ANIMATIONS) {
    animationStyleElement.textContent = GLOBAL_FREEZE_STYLES;
  } else {
    for (var i = 0; i < runningAnimations.length; i++) {
      runningAnimations[i].pause();
      frozenWaapiAnimations.push(runningAnimations[i]);
    }
  }
  var svgRoots = Array.from(document.querySelectorAll("svg"));
  pauseSvgAnimations(collectFrozenSvgElements(svgRoots), frozenSvgElements);
  installRafHold();
}

function unfreezeGlobalAnimations(cleanupErrors) {
  if (!hasGlobalAnimationFreeze) return;
  hasGlobalAnimationFreeze = false;

  if (animationStyleElement) {
    try {
      animationStyleElement.remove();
    } catch (error) {
      cleanupErrors.push(error);
    }
    animationStyleElement = null;
  }

  // play() resumes only the animations this freeze paused; removing the CSS
  // freeze sheet resumes the CSS ones. Nothing is finished.
  restorePausedAnimations(frozenWaapiAnimations, cleanupErrors);
  frozenWaapiAnimations = [];
  frozenSvgElements = resumeSvgAnimations(frozenSvgElements, cleanupErrors);
  try {
    uninstallRafHold();
  } catch (error) {
    cleanupErrors.push(error);
  }
}

// While frozen every page-scheduled requestAnimationFrame callback is held,
// whatever its shape; `off` flushes the held callbacks synchronously before it
// restores the replaced rAF pair. Held callbacks get negative ids so a
// cancelAnimationFrame reaches them while the hold is installed, and the ids
// stay live through the flush so one held callback can cancel another.
var isRafFrozen = false;
var isRafDraining = false;
var pendingRafCallbacks = new Map();
var nextFakeRafId = -1;
var frozenRafRequest = null;
var frozenRafCancel = null;
var didOwnRafRequest = false;
var didOwnRafCancel = false;

function installRafHold() {
  if (isRafFrozen) return;
  isRafFrozen = true;
  didOwnRafRequest = !!Object.getOwnPropertyDescriptor(window, "requestAnimationFrame");
  didOwnRafCancel = !!Object.getOwnPropertyDescriptor(window, "cancelAnimationFrame");
  frozenRafRequest = window.requestAnimationFrame;
  frozenRafCancel = window.cancelAnimationFrame;

  // A page can keep a reference to these wrappers past `off`; outside the hold
  // they delegate to the native pair they closed over.
  var nativeRequest = frozenRafRequest;
  var nativeCancel = frozenRafCancel;
  window.requestAnimationFrame = function (callback) {
    if (!isRafFrozen || isRafDraining) {
      return typeof nativeRequest === "function" ? nativeRequest.call(window, callback) : undefined;
    }
    var identifier = nextFakeRafId--;
    pendingRafCallbacks.set(identifier, callback);
    return identifier;
  };
  window.cancelAnimationFrame = function (identifier) {
    if (isRafFrozen && pendingRafCallbacks.has(identifier)) {
      pendingRafCallbacks.delete(identifier);
      return;
    }
    if (typeof nativeCancel === "function") nativeCancel.call(window, identifier);
  };
}

function uninstallRafHold() {
  if (!isRafFrozen) return;

  var heldCallbacks = [];
  pendingRafCallbacks.forEach(function (callback, identifier) {
    heldCallbacks.push({ identifier: identifier, callback: callback });
  });

  // The held ids stay live through the drain, so a callback can cancel a later
  // one; a re-schedule routes straight to the native pair and the wrappers are
  // uninstalled only once every held callback has run.
  isRafDraining = true;
  for (var i = 0; i < heldCallbacks.length; i++) {
    var held = heldCallbacks[i];
    if (!pendingRafCallbacks.has(held.identifier)) continue;
    pendingRafCallbacks.delete(held.identifier);
    try {
      held.callback.call(window, performance.now());
    } catch (error) {
      // A callback failure must reach window.onerror, not break the unfreeze.
      setTimeout(function () {
        throw error;
      }, 0);
    }
  }
  pendingRafCallbacks.clear();
  isRafDraining = false;
  isRafFrozen = false;

  if (didOwnRafRequest) window.requestAnimationFrame = frozenRafRequest;
  else delete window.requestAnimationFrame;
  if (didOwnRafCancel) window.cancelAnimationFrame = frozenRafCancel;
  else delete window.cancelAnimationFrame;
  frozenRafRequest = null;
  frozenRafCancel = null;
  didOwnRafRequest = false;
  didOwnRafCancel = false;
}

// ---------------------------------------------------------------------------
// Public seam
// ---------------------------------------------------------------------------

function reportFreezeErrors(context, errors) {
  if (errors.length > 0) console.warn("[omni-annotate] " + context, errors);
}

function unfreezeAll() {
  var cleanupErrors = [];
  try {
    if (isFreezeApplied) unfreezePseudoStates(cleanupErrors);
  } catch (error) {
    cleanupErrors.push(error);
  }
  try {
    unfreezeGlobalAnimations(cleanupErrors);
  } catch (error) {
    cleanupErrors.push(error);
  }
  return cleanupErrors;
}

ns.freeze = {
  on: function (x, y) {
    if (isFreezeApplied) return;
    var snapshot = collectPseudoStates(x, y);
    var runningAnimations = collectGlobalAnimationsToFreeze();
    try {
      applyPseudoStates(snapshot);
      applyGlobalAnimationFreeze(runningAnimations);
    } catch (error) {
      reportFreezeErrors("freezing failed", [error].concat(unfreezeAll()));
    }
  },
  off: function () {
    if (!isFreezeApplied && !hasGlobalAnimationFreeze) return;
    reportFreezeErrors("unfreezing failed", unfreezeAll());
  },
  openForHitTest: openForHitTest,
  closeAfterHitTest: closeAfterHitTest,
};
