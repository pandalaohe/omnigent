// SitePing DOM anchor utilities (MIT) — commit 3df02534a3dec57288b8b2445de951b21196ad5f,
// ported from dom_anchor.ts, dom_xpath.ts, dom_text-context.ts and
// dom_fingerprint.ts. Copyright (c) 2025 NeosiaNexus.
// Full licence text: THIRD_PARTY_NOTICES.md.

// Inlined @siteping/core constants (design §2.6 caps).
var ANCHOR_ELEMENT_ID_MAX = 191;
var ANCHOR_ELEMENT_TAG_MAX = 32;
var LABEL_MAX = 200;
var LABEL_DEPTH = 4;
var CSS_MAX = 700;
var XPATH_MAX = 900;
var QUOTE_EXACT_MAX = 200;
var QUOTE_CONTEXT_MAX = 32;
var FINGERPRINT_MAX = 120;
var NEIGHBOR_MAX = 80;
var ROLE_MAX = 64;
var ARIA_LABEL_MAX = 200;
var TEXT_MAX = 200;

/**
 * Joins the per-tree selectors of an element inside open shadow roots,
 * outermost host first (`my-card >>> .title`). Puppeteer's deep-descendant
 * notation; finder never emits it, since it escapes spaces and `>` inside
 * attribute values.
 */
var SHADOW_BOUNDARY = " >>> ";

/** Our own overlay host and hit-test shield, ignored as page content. */
var OVERLAY_HOST_ID = "__omni-annotate-host";
var OVERLAY_SHIELD_ID = "__omni-annotate-shield";

var FINDER_OPTIONS = {
  // Exclude framework-generated dynamic IDs
  idName: function (name) {
    return ns.finderWordLike(name) && !name.startsWith("radix-") && !/^:r[0-9]+:$/.test(name);
  },
  // Finder's default stable-attribute rule, plus the two host attributes
  // whose values are often opaque ids.
  attr: function (name, value) {
    return ns.finderAttr(name, value) || name === "data-testid" || name === "data-id";
  },
  // SitePing captures throw rather than stall the page; the wrapper below
  // drops only the CSS selector on failure.
  timeoutMs: 200,
  maxNumberOfPathChecks: 2000,
};

/**
 * True when `element` belongs to our annotation overlay: the marker host, the
 * hit-test shield, or anything inside either tree (shadow or light DOM).
 */
function isOverlayChrome(element) {
  var current = element;
  while (current) {
    if (
      current.nodeType === 1 &&
      (current.id === OVERLAY_HOST_ID || current.id === OVERLAY_SHIELD_ID)
    ) {
      return true;
    }
    var root = current.getRootNode ? current.getRootNode() : null;
    current = current.parentElement || (root && root.host ? root.host : null);
  }
  return false;
}

/** Like `element.parentElement`, but pierces shadow boundaries upwards. */
function parentElementCrossShadow(element) {
  if (element.parentElement) return element.parentElement;
  var root = element.getRootNode ? element.getRootNode() : null;
  return root && root.host ? root.host : null;
}

/** One-line `tag.class` segment for the label path. */
function labelSegment(element) {
  var tag = element.tagName.toLowerCase();
  var classes = [];
  for (var i = 0; i < element.classList.length && classes.length < 2; i++) {
    var name = element.classList[i];
    if (ns.finderWordLike(name)) classes.push(name);
  }
  return classes.length > 0 ? tag + "." + classes.join(".") : tag;
}

/** Short one-line path of up to LABEL_DEPTH ancestors, capped with `…`. */
function buildLabel(element) {
  var parts = [];
  var current = element;
  while (current && parts.length < LABEL_DEPTH) {
    parts.unshift(labelSegment(current));
    current = parentElementCrossShadow(current);
  }
  return capString(parts.join(" > "), LABEL_MAX);
}

function capString(value, max) {
  return value.length > max ? value.slice(0, max - 1) + "…" : value;
}

// ---------------------------------------------------------------------------
// text-context.ts
// ---------------------------------------------------------------------------

/** Raw-char budget for sibling reads that only keep 32–40 chars — generous
 * headroom for leading/trailing whitespace that trimming discards. */
var SIBLING_READ_CAP = 256;

/**
 * `sibling`, or the nearest page element past it in `prop` direction. Our own
 * overlay host is appended after the page and its text changes with marker
 * count — as anchor context it would drift, and be empty on reload.
 */
function pageSibling(sibling, prop) {
  var current = sibling;
  while (current && isOverlayChrome(current)) current = current[prop];
  return current;
}

/** The host's privacy mask, mirrored from SitePing; masked text is never read. */
function isMasked(node) {
  return node.nodeType === 1 && node.getAttribute("data-siteping-ignore") === "true";
}

/** TreeWalker filter for `skipMasked`: text nodes only, a masked subtree rejected whole. */
function rejectMasked(node) {
  if (node.nodeType === 3) return NodeFilter.FILTER_ACCEPT;
  return isMasked(node) ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_SKIP;
}

/**
 * Extract ~32 chars of text from the nearest sibling with content.
 * Walks up to 3 siblings in the given direction.
 *
 * Sibling text is read through the bounded walkers, never `textContent` — a
 * sibling can be an arbitrarily large subtree, and serializing it wholesale to
 * keep 32 chars is the dominant hidden cost of candidate verification.
 */
function adjacentText(element, direction) {
  var prop = direction === "before" ? "previousElementSibling" : "nextElementSibling";
  var sibling = pageSibling(element[prop], prop);
  var attempts = 3;

  while (sibling && attempts > 0) {
    var text =
      direction === "before"
        ? boundedTextEnd(sibling, SIBLING_READ_CAP, true).trim()
        : boundedText(sibling, SIBLING_READ_CAP, true).trim();
    if (text) {
      return direction === "before" ? text.slice(-32) : text.slice(0, 32);
    }
    sibling = pageSibling(sibling[prop], prop);
    attempts--;
  }

  return "";
}

/** Immediate (page, not overlay) siblings' text for disambiguation, masked text excluded. */
function neighborText(element) {
  var prevSibling = pageSibling(element.previousElementSibling, "previousElementSibling");
  var nextSibling = pageSibling(element.nextElementSibling, "nextElementSibling");
  var prev = prevSibling
    ? boundedText(prevSibling, SIBLING_READ_CAP, true).trim().slice(0, 40)
    : "";
  var next = nextSibling
    ? boundedText(nextSibling, SIBLING_READ_CAP, true).trim().slice(0, 40)
    : "";
  return [prev, next].filter(Boolean).join(" | ");
}

/**
 * First `cap` characters of an element's text, without serializing the whole
 * subtree. `textContent.slice(0, cap)` still pays for full subtree
 * serialization first — repeated across scan candidates it degenerates to
 * O(page²). A TreeWalker yields the same text nodes in the same (tree) order
 * and stops as soon as the budget is reached.
 */
function boundedText(element, cap, skipMasked) {
  var out = "";

  // Leaf fast path — the majority of scan candidates have no element children.
  if (element.firstElementChild === null) {
    for (var i = 0; i < element.childNodes.length; i++) {
      var node = element.childNodes[i];
      if (node.nodeType === 3) {
        out += node.data;
        if (out.length >= cap) break;
      }
    }
    return out.length > cap ? out.slice(0, cap) : out;
  }

  var walker = skipMasked
    ? element.ownerDocument.createTreeWalker(
        element,
        NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT,
        rejectMasked,
      )
    : element.ownerDocument.createTreeWalker(element, NodeFilter.SHOW_TEXT);
  while (out.length < cap) {
    var nextNode = walker.nextNode();
    if (!nextNode) break;
    out += nextNode.data;
  }
  return out.length > cap ? out.slice(0, cap) : out;
}

/**
 * Last `cap` characters of an element's text — the reverse-direction
 * counterpart of `boundedText` (TreeWalker only walks forward).
 */
function boundedTextEnd(element, cap, skipMasked) {
  var out = "";
  var walk = function (node) {
    for (var child = node.lastChild; child; child = child.previousSibling) {
      if (child.nodeType === 3) {
        out = child.data + out;
        if (out.length >= cap) return true;
      } else if (child.nodeType === 1 && !(skipMasked && isMasked(child)) && walk(child)) {
        return true;
      }
    }
    return false;
  };
  walk(element);
  return out.length > cap ? out.slice(-cap) : out;
}

// ---------------------------------------------------------------------------
// fingerprint.ts
// ---------------------------------------------------------------------------

var STABLE_ATTRS = ["role", "aria-label", "type", "name", "href", "src", "data-testid", "data-id"];

/** Simple 32-bit hash (djb2). */
function djb2(str) {
  var hash = 5381;
  for (var i = 0; i < str.length; i++) {
    hash = ((hash << 5) + hash + str.charCodeAt(i)) | 0;
  }
  return (hash >>> 0).toString(36);
}

/**
 * Generate a compact structural fingerprint for a DOM element.
 *
 * Format: `"childCount:siblingIdx:attrHash"`. Tag name is NOT included — it is
 * stored separately in `target.tag`.
 */
function generateFingerprint(element) {
  var childCount = element.children.length;

  var siblingIdx = 0;
  var parent = element.parentElement;
  if (parent) {
    for (var i = 0; i < parent.children.length; i++) {
      var child = parent.children[i];
      if (child === element) break;
      if (child.tagName === element.tagName) siblingIdx++;
    }
  }

  return childCount + ":" + siblingIdx + ":" + attrHash(element);
}

/**
 * Stable-attribute hash component of the fingerprint, exposed separately:
 * unlike the sibling-index component it is O(1) per element, so scan
 * prefiltering can afford it on every candidate.
 */
function attrHash(element) {
  var attrs = [];
  for (var i = 0; i < STABLE_ATTRS.length; i++) {
    var value = element.getAttribute(STABLE_ATTRS[i]);
    if (value) attrs.push(STABLE_ATTRS[i] + "=" + value);
  }
  return attrs.length > 0 ? djb2(attrs.join(",")) : "0";
}

/**
 * Score how well a candidate element matches a stored fingerprint (0–1).
 * Child count match: 0.2 (tolerant), sibling index: 0.4 (positional),
 * attribute hash: 0.4 (identity).
 */
function scoreFingerprint(candidate, storedFingerprint) {
  var parts = storedFingerprint.split(":");
  if (parts.length !== 3) return 0;

  var storedChildCount = Number(parts[0]);
  var storedSibIndex = Number(parts[1]);
  var storedAttrHash = parts[2];
  if (Number.isNaN(storedChildCount) || Number.isNaN(storedSibIndex)) return 0;

  var candidateFp = generateFingerprint(candidate);
  var candParts = candidateFp.split(":");
  var candChildren = Number(candParts[0]);
  var candSibIdx = Number(candParts[1]);
  var candAttrHash = candParts[2];

  var score = 0;

  var childDiff = Math.abs(candChildren - storedChildCount);
  if (childDiff === 0) score += 0.2;
  else if (childDiff <= 2) score += 0.1;
  else if (childDiff <= 5) score += 0.03;

  var sibDiff = Math.abs(candSibIdx - storedSibIndex);
  if (sibDiff === 0) score += 0.4;
  else if (sibDiff === 1) score += 0.2;
  else if (sibDiff <= 3) score += 0.08;

  if (candAttrHash === storedAttrHash) score += 0.4;

  return score;
}

// ---------------------------------------------------------------------------
// xpath.ts
// ---------------------------------------------------------------------------

/**
 * Generate an optimized XPath for a DOM element. Unique id → `//tag[@id=…]`;
 * otherwise walk up to 6 levels building `/tag[position]` segments until an
 * ancestor with an id or `<body>`; a path that does not reach `<body>` is
 * emitted relative (`/tag[n]/…`). Inside a shadow tree: rooted at its shadow
 * root (see shadowXPath).
 */
function generateXPath(element) {
  if (element.getRootNode() instanceof ShadowRoot) return shadowXPath(element);

  if (element.id) {
    return "//" + element.localName + "[@id=" + xpathLiteral(element.id) + "]";
  }

  var segments = [];
  var current = element;

  while (current && current !== document.body && segments.length < 6) {
    var tag = current.localName;
    var parent = current.parentElement;

    if (current.id) {
      segments.unshift("/" + tag + "[@id=" + xpathLiteral(current.id) + "]");
      return "/" + segments.join("");
    }

    var position = 1;
    if (parent) {
      for (var i = 0; i < parent.children.length; i++) {
        var sibling = parent.children[i];
        if (sibling === current) break;
        if (sibling.localName === tag) position++;
      }
    }

    segments.unshift("/" + tag + "[" + position + "]");
    current = parent;
  }

  // The walk stopped short of <body>: the relative form matches the element
  // wherever it sits — possibly alongside look-alikes, which the resolver
  // gathers and verifies like CSS matches. Shadow-tree elements never get
  // here (see shadowXPath).
  if (current !== document.body) return "/" + segments.join("");
  return "/html/body" + segments.join("");
}

/**
 * XPath cannot enter shadow trees, so a shadow element's path is informational:
 * the resolver never evaluates it. Walked up to the shadow root, with no `//`
 * shortcut and no depth cap, so a light-DOM look-alike can never match.
 */
function shadowXPath(element) {
  var path = "";
  for (var current = element; current; current = current.parentElement) {
    var tag = current.localName;
    if (current.id) {
      path = "/" + tag + "[@id=" + xpathLiteral(current.id) + "]" + path;
      continue;
    }
    // Siblings, not parent.children: a top-level element's parent is the
    // shadow root, which parentElement skips.
    var position = 1;
    for (
      var sibling = current.previousElementSibling;
      sibling;
      sibling = sibling.previousElementSibling
    ) {
      if (sibling.localName === tag) position++;
    }
    path = "/" + tag + "[" + position + "]" + path;
  }
  return "." + path;
}

/** An XPath string literal for `value` (`concat()` when it holds a quote). */
function xpathLiteral(value) {
  return value.includes("'")
    ? "concat('" + value.replace(/'/g, "',\"'\",'") + "')"
    : "'" + value + "'";
}

// ---------------------------------------------------------------------------
// anchor.ts
// ---------------------------------------------------------------------------

/**
 * Generate a multi-selector target for a DOM element, consumed by
 * `resolveTarget`: CSS (finder, one selector per shadow tree joined by
 * SHADOW_BOUNDARY), XPath, text quote with prefix/suffix context, structural
 * fingerprint, neighbor text and a human label.
 */
function generateTarget(element) {
  var css;
  try {
    var selectors = [];
    var current = element;
    while (current) {
      selectors.unshift(ns.finder(current, FINDER_OPTIONS));
      var root = current.getRootNode ? current.getRootNode() : null;
      current = root && root.host ? root.host : null;
    }
    css = selectors.join(SHADOW_BOUNDARY);
  } catch {
    // finder found no unique selector; xpath + quote + fingerprint remain.
    css = "";
  }

  var rawText = (element.textContent || "").trim();
  return {
    label: buildLabel(element),
    css: capString(css, CSS_MAX),
    xpath: capString(generateXPath(element), XPATH_MAX),
    quote: {
      exact: capString(rawText, QUOTE_EXACT_MAX),
      prefix: capString(adjacentText(element, "before"), QUOTE_CONTEXT_MAX),
      suffix: capString(adjacentText(element, "after"), QUOTE_CONTEXT_MAX),
    },
    fingerprint: capString(generateFingerprint(element), FINGERPRINT_MAX),
    neighborText: capString(neighborText(element), NEIGHBOR_MAX),
    tag: element.tagName.slice(0, ANCHOR_ELEMENT_TAG_MAX),
    // Over-long ids are dropped, not truncated: a truncated id matches nothing
    // (or the wrong element) — the resolver falls back to other strategies.
    id: element.id && element.id.length <= ANCHOR_ELEMENT_ID_MAX ? element.id : "",
    role: capString(element.getAttribute("role") || "", ROLE_MAX),
    ariaLabel: capString(element.getAttribute("aria-label") || "", ARIA_LABEL_MAX),
    text: capString(rawText, TEXT_MAX),
  };
}

ns.generateTarget = generateTarget;
ns.anchorHelpers = {
  SHADOW_BOUNDARY: SHADOW_BOUNDARY,
  attrHash: attrHash,
  scoreFingerprint: scoreFingerprint,
  boundedText: boundedText,
  adjacentText: adjacentText,
  neighborText: neighborText,
  isOverlayChrome: isOverlayChrome,
};
