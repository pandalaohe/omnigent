// In-frame comment bridge for the HTML artifact preview: the server inlines
// these bytes and htmlCommentBridge.ts imports them ?raw. Dependency-free; it
// runs in the artifact's opaque-origin frame and no-ops without a nonce.

(function () {
  var NONCE = (document.currentScript && document.currentScript.dataset.omniNonce) || "";
  if (!NONCE) return;

  var SRC = "omni-html-comment";
  var T = {
    init: "omni:init",
    ready: "omni:ready",
    setComments: "omni:setComments",
    setActive: "omni:setActive",
    selection: "omni:selection",
    commentClick: "omni:commentClick",
    selectionCleared: "omni:selectionCleared",
    openPath: "omni:openPath",
    orphans: "omni:orphans",
  };

  // The highlight styles live here rather than in the injected markup so the
  // server only has to inject scripts.
  var style = document.createElement("style");
  style.textContent =
    "::highlight(omni-comment){background-color:rgba(250,204,21,0.25);}" +
    "::highlight(omni-comment-active){background-color:rgba(250,204,21,0.5);}";
  (document.head || document.documentElement).appendChild(style);

  var port = null;
  // The visitor shell's init marks visit mode: link clicks must keep the shell
  // in place, unlike the panel's own link routing.
  var VISIT = false;
  var comments = [];
  var active = null;
  var activeRanges = []; // ranges matching the active comment (for scroll-into-view)
  var ranges = []; // [{ id, range }] for click hit-testing

  function send(msg) {
    if (!port) return;
    msg.source = SRC;
    msg.nonce = NONCE;
    try {
      port.postMessage(msg);
    } catch {
      // The parent may have closed the port between messages; dropping is safe.
    }
  }

  function blockOwner(node) {
    var el = node.parentElement;
    while (el && el !== document.body) {
      if (
        /^(P|DIV|SECTION|ARTICLE|HEADER|FOOTER|MAIN|ASIDE|LI|PRE|BLOCKQUOTE|H[1-6]|TD|TH|TR)$/i.test(
          el.tagName,
        )
      )
        return el;
      el = el.parentElement;
    }
    return document.body;
  }

  function blockGap(previous, next) {
    var a = blockOwner(previous);
    var b = blockOwner(next);
    if (a === b) return "";
    return a.tagName === "P" || b.tagName === "P" ? "\n\n" : "\n";
  }

  // Flat rendered-text index. A block gap is indexed as text so a selection
  // across paragraphs matches the quote sent to the parent.
  function buildIndex() {
    var walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, {
      acceptNode: function (n) {
        var p = n.parentElement;
        if (!p) return NodeFilter.FILTER_REJECT;
        var tag = p.tagName;
        if (tag === "SCRIPT" || tag === "STYLE" || tag === "NOSCRIPT") {
          return NodeFilter.FILTER_REJECT;
        }
        var visibility = window.getComputedStyle(p).visibility;
        if (visibility === "hidden" || visibility === "collapse") return NodeFilter.FILTER_REJECT;
        for (var el = p; el; el = el.parentElement) {
          if (window.getComputedStyle(el).display === "none") return NodeFilter.FILTER_REJECT;
        }
        return NodeFilter.FILTER_ACCEPT;
      },
    });
    var nodes = [];
    var text = "";
    var n;
    while ((n = walker.nextNode())) {
      if (nodes.length) text += blockGap(nodes[nodes.length - 1].node, n);
      nodes.push({ node: n, start: text.length });
      text += n.nodeValue;
    }
    return { nodes: nodes, text: text };
  }

  function locate(nodes, pos) {
    for (var i = 0; i < nodes.length; i++) {
      if (pos < nodes[i].start) return { node: nodes[i].node, offset: 0 };
      var len = nodes[i].node.nodeValue.length;
      if (pos <= nodes[i].start + len) {
        return { node: nodes[i].node, offset: pos - nodes[i].start };
      }
    }
    var last = nodes[nodes.length - 1];
    return last ? { node: last.node, offset: last.node.nodeValue.length } : null;
  }

  function contextBefore(text, end) {
    var start = Math.max(0, end - 32);
    if (
      start > 0 &&
      /[\uDC00-\uDFFF]/.test(text.charAt(start)) &&
      /[\uD800-\uDBFF]/.test(text.charAt(start - 1))
    )
      start++;
    return text.slice(start, end);
  }

  function contextAfter(text, start) {
    var end = Math.min(text.length, start + 32);
    if (
      end < text.length &&
      /[\uD800-\uDBFF]/.test(text.charAt(end - 1)) &&
      /[\uDC00-\uDFFF]/.test(text.charAt(end))
    )
      end--;
    return text.slice(start, end);
  }

  // Whitespace-normalized view of a string: runs of whitespace collapse to a
  // single space, with a map from each normalized index back to its raw offset
  // (plus a trailing sentinel = raw length). charAt(i) <= " " treats every code
  // point <= U+0020 (space, tab, CR/LF, FF) as whitespace without needing regex.
  function normWs(text) {
    var norm = "";
    var map = [];
    var prevSpace = false;
    for (var i = 0; i < text.length; i++) {
      var ch = text.charAt(i);
      if (ch <= " ") {
        if (prevSpace) continue;
        norm += " ";
        map.push(i);
        prevSpace = true;
      } else {
        norm += ch;
        map.push(i);
        prevSpace = false;
      }
    }
    map.push(text.length);
    return { norm: norm, map: map };
  }

  // All ranges matching the anchor. anchor_content is rendered-selection text
  // (whitespace collapsed) but the haystack is raw text-node data that preserves
  // the source's newlines/indentation, so match on the normalized view and map
  // normalized offsets back to raw node positions. H (the normalized view of
  // index.text) is passed in so repaint builds it once for all comments.
  function anchorRanges(index, H, anchor, overlapping) {
    var out = [];
    var raw = (anchor || "").trim();
    if (!raw) return out;
    var needle = normWs(raw).norm.trim();
    if (!needle) return out;
    var from = 0;
    var guard = 0;
    while (guard++ < 1000) {
      var at = H.norm.indexOf(needle, from);
      if (at === -1) break;
      var s = locate(index.nodes, H.map[at]);
      var e = locate(index.nodes, H.map[at + needle.length]);
      if (s && e) {
        var r = document.createRange();
        try {
          r.setStart(s.node, s.offset);
          r.setEnd(e.node, e.offset);
          out.push(r);
        } catch {
          // A boundary that no longer resolves to a valid range is skipped.
        }
      }
      from = at + (overlapping ? 1 : Math.max(1, needle.length));
    }
    return out;
  }

  function selectorFor(node) {
    var el = node.nodeType === 1 ? node : node.parentElement;
    var parts = [];
    while (el && el !== document.body && parts.length < 20) {
      if (el.id && typeof CSS !== "undefined" && CSS.escape) {
        var id = "#" + CSS.escape(el.id);
        if (document.querySelectorAll(id).length === 1) {
          parts.unshift(id);
          break;
        }
      }
      var ordinal = 1;
      for (var prev = el.previousElementSibling; prev; prev = prev.previousElementSibling) {
        if (prev.tagName === el.tagName) ordinal++;
      }
      parts.unshift(el.tagName.toLowerCase() + ":nth-of-type(" + ordinal + ")");
      el = el.parentElement;
    }
    if (el === document.body) parts.unshift("body");
    else if (el && parts.length >= 20) return undefined;
    var selector = parts.join(" > ");
    if (!selector || selector.length > 700) return undefined;
    try {
      var matches = document.querySelectorAll(selector);
      return matches.length === 1 ? selector : undefined;
    } catch {
      return undefined;
    }
  }

  function matchingRanges(index, H, anchor) {
    if (!anchor || typeof anchor.text !== "string" || !anchor.text.trim()) return [];
    var needle = normWs(anchor.text.trim()).norm.trim();
    var prefix = normWs(anchor.prefix || "").norm;
    var suffix = normWs(anchor.suffix || "").norm;
    var match = -1;
    var from = 0;
    while (from <= H.norm.length) {
      var at = H.norm.indexOf(needle, from);
      if (at < 0) break;
      var end = at + needle.length;
      if (
        H.norm.slice(Math.max(0, at - prefix.length), at) === prefix &&
        H.norm.slice(end, end + suffix.length) === suffix
      ) {
        if (match !== -1) return [];
        match = at;
      }
      from = at + 1;
    }
    if (match === -1) return [];
    var s = locate(index.nodes, H.map[match]);
    var e = locate(index.nodes, H.map[match + needle.length]);
    if (!s || !e) return [];
    var range = document.createRange();
    try {
      range.setStart(s.node, s.offset);
      range.setEnd(e.node, e.offset);
    } catch {
      return [];
    }
    return [range];
  }

  // Flat raw-text offset of a selection boundary (node, offset) within the
  // index's concatenated text. When the boundary is a text node we map directly;
  // when it's an element (e.g. selecting a whole <span>, the boundary is the
  // parent with a child index) we return the flat start of the first indexed
  // text node at or after that boundary, using a collapsed range to compare
  // document order. Returns -1 if nothing matches.
  function flatOffset(index, node, offset) {
    if (node.nodeType === 3) {
      for (var i = 0; i < index.nodes.length; i++) {
        if (index.nodes[i].node === node) return index.nodes[i].start + offset;
      }
      return -1;
    }
    var boundary = document.createRange();
    try {
      boundary.setStart(node, offset);
    } catch {
      return -1;
    }
    for (var k = 0; k < index.nodes.length; k++) {
      var tn = index.nodes[k].node;
      // First text node that starts at or after the boundary.
      if (boundary.comparePoint(tn, 0) >= 0) return index.nodes[k].start;
    }
    return index.text.length;
  }

  // Which occurrence (0-based, document order) of the selected text the current
  // selection is, so the parent can anchor to the copy actually selected rather
  // than the first text match. Counts normalized matches starting before the
  // selection's start — mirrors anchorRanges/anchorOccurrence so a wrapped
  // occurrence still counts. Returns 0 if the position can't be resolved.
  function selectionAnchor(range, text) {
    var index = buildIndex();
    var start = flatOffset(index, range.startContainer, range.startOffset);
    var end = flatOffset(index, range.endContainer, range.endOffset);
    if (start < 0 || end <= start) return null;
    var H = normWs(index.text);
    var needle = normWs((text || "").trim()).norm.trim();
    if (!needle) return null;
    // Native selection is authoritative. If our rendered index cannot account
    // for its text, keep the selection unresolved rather than invent an excerpt.
    if (normWs(index.text.slice(start, end)).norm.trim() !== needle) return null;
    var count = 0;
    var from = 0;
    while (from <= H.norm.length) {
      var at = H.norm.indexOf(needle, from);
      if (at === -1) break;
      if (H.map[at] >= start) {
        return {
          occ: count,
          text: text,
          prefix: contextBefore(H.norm, at),
          suffix: contextAfter(H.norm, at + needle.length),
          selector: selectorFor(range.commonAncestorContainer),
        };
      }
      count++;
      from = at + 1;
    }
    return null;
  }

  function repaint() {
    var supported =
      typeof CSS !== "undefined" && CSS.highlights && typeof Highlight !== "undefined";
    var index = buildIndex();
    // Normalize the document text once and reuse it for every comment, rather
    // than rebuilding the whitespace map per comment inside anchorRanges.
    var H = normWs(index.text);
    ranges = [];
    var base = [];
    var activeHi = [];
    var orphans = [];
    for (var i = 0; i < comments.length; i++) {
      var c = comments[i];
      var picked = c.anchor
        ? matchingRanges(index, H, c.anchor)
        : typeof c.occ === "number" && c.occ >= 0
          ? anchorRanges(index, H, c.anchor_content).slice(c.occ, c.occ + 1)
          : [];
      if (!picked.length && c.id) orphans.push(c.id);
      var isActive = active && active.comment_id === c.id;
      for (var j = 0; j < picked.length; j++) {
        ranges.push({ id: c.id, range: picked[j] });
        if (isActive) activeHi.push(picked[j]);
        else base.push(picked[j]);
      }
    }
    activeRanges = activeHi;
    send({ type: T.orphans, ids: orphans });
    if (!supported) return;
    try {
      CSS.highlights.set("omni-comment", new Highlight(...base.filter(Boolean)));
      CSS.highlights.set("omni-comment-active", new Highlight(...activeHi.filter(Boolean)));
    } catch {
      // Custom Highlight support can be partial; losing paint is non-fatal.
    }
  }

  // Scroll the first active-comment range into view. Uses the range's client
  // rect (Ranges have no scrollIntoView) to center it in the viewport, but only
  // when off-screen so an already-visible highlight doesn't jump.
  function scrollActiveIntoView() {
    var r = activeRanges && activeRanges[0];
    if (!r) return;
    var rect = r.getBoundingClientRect();
    if (!rect || (rect.width === 0 && rect.height === 0)) return;
    var vh = window.innerHeight || document.documentElement.clientHeight;
    if (rect.top >= 0 && rect.bottom <= vh) return; // already fully visible
    var target = window.pageYOffset + rect.top - vh / 2 + rect.height / 2;
    window.scrollTo({ top: target < 0 ? 0 : target, behavior: "smooth" });
  }

  function rectOf(range) {
    var list = range.getClientRects();
    var r = list && list.length ? list[0] : range.getBoundingClientRect();
    return { left: r.left, top: r.top, right: r.right, bottom: r.bottom };
  }

  function caretRange(x, y) {
    if (document.caretRangeFromPoint) return document.caretRangeFromPoint(x, y);
    if (document.caretPositionFromPoint) {
      var p = document.caretPositionFromPoint(x, y);
      if (!p) return null;
      var r = document.createRange();
      r.setStart(p.offsetNode, p.offset);
      r.collapse(true);
      return r;
    }
    return null;
  }

  // The current non-empty selection, or null if collapsed/empty.
  function currentSelection() {
    var sel = window.getSelection();
    if (!sel || sel.rangeCount === 0) return null;
    var text = sel.toString();
    if (sel.isCollapsed || !text.trim()) return null;
    return { range: sel.getRangeAt(0), text: text };
  }

  function emitSelection() {
    var s = currentSelection();
    if (s) {
      var anchor = selectionAnchor(s.range, s.text);
      if (!anchor) return;
      send({
        type: T.selection,
        text: anchor.text,
        occ: anchor.occ,
        prefix: anchor.prefix,
        suffix: anchor.suffix,
        selector: anchor.selector,
        rect: rectOf(s.range),
      });
    }
  }

  // Drop the native selection once it's covered by a saved comment, so the
  // browser's ::selection stops masking the (lower-priority) Custom Highlight.
  // Matches on normalized text so collapsed rendered whitespace still compares
  // equal to the comment's stored anchor_content.
  function clearSelectionIfCommented() {
    var s = currentSelection();
    if (!s) return;
    var selText = normWs(s.text).norm.trim();
    if (!selText) return;
    for (var i = 0; i < comments.length; i++) {
      if (normWs(comments[i].anchor_content || "").norm.trim() === selText) {
        var sel = window.getSelection();
        if (sel) sel.removeAllRanges();
        return;
      }
    }
  }

  function onMouseUp(e) {
    var s = currentSelection();
    if (s) {
      var anchor = selectionAnchor(s.range, s.text);
      if (!anchor) return;
      send({
        type: T.selection,
        text: anchor.text,
        occ: anchor.occ,
        prefix: anchor.prefix,
        suffix: anchor.suffix,
        selector: anchor.selector,
        rect: rectOf(s.range),
      });
      return;
    }
    // A plain click (collapsed selection) — did it land inside a comment range?
    var cr = caretRange(e.clientX, e.clientY);
    if (cr) {
      for (var i = 0; i < ranges.length; i++) {
        if (ranges[i].range.isPointInRange(cr.startContainer, cr.startOffset)) {
          send({ type: T.commentClick, id: ranges[i].id });
          return;
        }
      }
    }
    send({ type: T.selectionCleared });
  }

  // A plain click on a link. Panel mode keeps the top page out of it: an
  // in-bundle link whose effective target (`target`, else the first
  // `<base target>`) is `_top`/`_parent`/`_blank` navigates the frame itself;
  // a link naming another workspace file is posted to the parent to open in the
  // file viewer; an external site opens a new tab; other in-bundle clicks stay
  // native. Visit mode: cross-origin -> tab, `_top`/`_parent` -> frame.
  function onDocumentClick(e) {
    if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.defaultPrevented) {
      return;
    }
    var target = e.target;
    if (!target || target.nodeType !== 1) return;
    var anchor = target.closest("a[href]");
    if (!anchor) return;
    // The first `v1/artifacts` marks the route boundary; a bundle's own files
    // may contain that segment again deeper in the path.
    var m = /^(.*?\/v1\/artifacts\/[^/]+\/)/.exec(location.pathname);
    if (!m) return;
    var url;
    try {
      url = new URL(anchor.href, location.href);
    } catch {
      return;
    }
    var sameOrigin = url.protocol === location.protocol && url.host === location.host;
    var inBundle = sameOrigin && url.pathname.indexOf(m[1]) === 0;
    if (!VISIT) {
      // A workspace file outside this artifact: the parent resolves the raw
      // href against its own base, since URL resolution clamps a `..` above
      // the token segment and would lose the target.
      if (url.protocol === "file:" || (sameOrigin && !inBundle)) {
        e.preventDefault();
        send({ type: T.openPath, href: anchor.getAttribute("href"), base: document.baseURI });
        return;
      }
      if (url.protocol !== "http:" && url.protocol !== "https:") return;
      if (inBundle) {
        // Capture runs before the page's own handlers, so only a target that
        // would leave the frame is taken over; a download stays native.
        var effectiveTarget = anchor.getAttribute("target");
        if (effectiveTarget === null) {
          var baseEl = document.querySelector("base[target]");
          effectiveTarget = baseEl ? baseEl.getAttribute("target") : "";
        }
        effectiveTarget = (effectiveTarget || "").toLowerCase();
        if (
          !anchor.hasAttribute("download") &&
          (effectiveTarget === "_top" ||
            effectiveTarget === "_parent" ||
            effectiveTarget === "_blank")
        ) {
          e.preventDefault();
          window.location.assign(url.href);
        }
        return;
      }
      e.preventDefault();
      window.open(url.href, "_blank", "noopener,noreferrer");
      return;
    }
    if (url.protocol !== "http:" && url.protocol !== "https:") return;
    var linkTarget = (anchor.getAttribute("target") || "").toLowerCase();
    if (sameOrigin && (linkTarget === "_top" || linkTarget === "_parent")) {
      e.preventDefault();
      window.location.assign(url.href);
      return;
    }
    if (!sameOrigin) {
      e.preventDefault();
      window.open(url.href, "_blank", "noopener");
      return;
    }
    if (inBundle) {
      return;
    }
    e.preventDefault();
    window.open(url.href, "_blank", "noopener");
  }

  // Also react to programmatic / keyboard selection (mouseup alone misses
  // these, and Playwright's select_text drives selection without a mouse).
  // Debounced; only emits for a non-empty selection so a collapse here never
  // clears the active comment (mouseup owns the clear path).
  var selTimer = null;
  document.addEventListener("selectionchange", function () {
    if (selTimer) clearTimeout(selTimer);
    selTimer = setTimeout(emitSelection, 150);
  });

  window.addEventListener("message", function (e) {
    var d = e.data;
    if (!d || d.source !== SRC || d.nonce !== NONCE) return;
    if (d.type === T.init && e.ports && e.ports[0]) {
      VISIT = d.visit === true;
      port = e.ports[0];
      port.onmessage = function (ev) {
        var m = ev.data;
        if (!m) return;
        if (m.type === T.setComments) {
          comments = Array.isArray(m.comments) ? m.comments : [];
          // If a newly-arrived comment covers the still-active native selection
          // (i.e. the user just saved a comment on it), drop that selection.
          // The browser's ::selection paints over Custom Highlights, so the range
          // would stay grey — masking the yellow highlight — until the user
          // clicked elsewhere to collapse it. Keeping the selection during
          // compose is intentional; we only clear once the comment exists.
          clearSelectionIfCommented();
          repaint();
        } else if (m.type === T.setActive) {
          var next = m.active && m.active.anchor_content ? m.active : null;
          var prevKey = active
            ? active.comment_id || active.anchor_content + "#" + active.occ
            : null;
          var nextKey = next ? next.comment_id || next.anchor_content + "#" + next.occ : null;
          active = next;
          repaint();
          // Only scroll when a comment becomes newly active (e.g. clicked in the
          // panel), so list refreshes that keep the same active comment don't
          // yank the reader's scroll position.
          if (nextKey && nextKey !== prevKey) scrollActiveIntoView();
        }
      };
      send({ type: T.ready, pathname: location.pathname });
    }
  });

  document.addEventListener("click", onDocumentClick, true);
  document.addEventListener("mouseup", onMouseUp, true);
})();
