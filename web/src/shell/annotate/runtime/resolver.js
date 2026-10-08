// SitePing DOM resolver (MIT) — commit 3df02534a3dec57288b8b2445de951b21196ad5f,
// ported from dom_resolver.ts, dom_fuzzy.ts and dom_visibility.ts.
// Copyright (c) 2025 NeosiaNexus.
// Full licence text: THIRD_PARTY_NOTICES.md.

var SHADOW_BOUNDARY = ns.anchorHelpers.SHADOW_BOUNDARY;
var attrHash = ns.anchorHelpers.attrHash;
var scoreFingerprint = ns.anchorHelpers.scoreFingerprint;
var boundedText = ns.anchorHelpers.boundedText;
var adjacentText = ns.anchorHelpers.adjacentText;
var neighborText = ns.anchorHelpers.neighborText;
var isOverlayChrome = ns.anchorHelpers.isOverlayChrome;

// ---------------------------------------------------------------------------
// fuzzy.ts
// ---------------------------------------------------------------------------

/** Below this needle length, approximate substring search over a LONG
 * haystack is statistically meaningless (a 4-char needle "matches" almost
 * anything within 1–2 edits somewhere in 500 chars). Exact containment still
 * applies at any length, and short haystacks keep approximate matching — a
 * near-miss in a short label ("panier" vs "paniers") is meaningful. */
var MIN_FUZZY_NEEDLE_LENGTH = 8;

var SHORT_NEEDLE_HAYSTACK_CAP = 64;

var HAYSTACK_CAP = 500;

/**
 * Normalize text for comparison: Unicode NFC + collapse whitespace runs to a
 * single space + trim. Absorbs SSR/CSR hydration drift and re-indentation.
 * Deliberately NOT case-folding: a case change is a real signal.
 */
function normalizeText(s) {
  return s.normalize("NFC").replace(/\s+/g, " ").trim();
}

/** Cheap whitespace-only collapse for the scan prefilter (NFC is a
 * table-driven full-Unicode pass and is skipped there; survivors re-normalize). */
function collapseWhitespace(s) {
  return s.replace(/\s+/g, " ").trim();
}

/** Levenshtein edit distance. O(n*m) time, O(min(n,m)) space. */
function editDistance(a, b) {
  if (a === b) return 0;
  if (a.length === 0) return b.length;
  if (b.length === 0) return a.length;

  var short = a.length > b.length ? b : a;
  var long = a.length > b.length ? a : b;

  var aLen = short.length;
  var bLen = long.length;
  var prev = new Array(aLen + 1);
  for (var k = 0; k <= aLen; k++) prev[k] = k;
  var curr = new Array(aLen + 1);

  for (var j = 1; j <= bLen; j++) {
    curr[0] = j;
    for (var i = 1; i <= aLen; i++) {
      var prevDiag = prev[i - 1];
      curr[i] =
        short[i - 1] === long[j - 1] ? prevDiag : 1 + Math.min(prevDiag, prev[i], curr[i - 1]);
    }
    var tmp = prev;
    prev = curr;
    curr = tmp;
  }

  return prev[aLen];
}

/** Normalized similarity score (0–1, where 1 = identical). */
function similarity(a, b) {
  if (a === b) return 1;
  var maxLen = Math.max(a.length, b.length);
  if (maxLen === 0) return 1;
  return 1 - editDistance(a, b) / maxLen;
}

/**
 * Sellers 1980 — minimum edit distance between `needle` and any substring of
 * `haystack`. Row 0 of the DP is zero for every haystack position (a match may
 * start anywhere); the answer is the running minimum of the last row.
 */
function sellersDistance(haystack, needle) {
  var n = needle.length;
  var prev = new Int32Array(n + 1);
  var curr = new Int32Array(n + 1);
  for (var i = 0; i <= n; i++) prev[i] = i;
  var best = n;

  for (var j = 1; j <= haystack.length; j++) {
    curr[0] = 0;
    var hc = haystack.charCodeAt(j - 1);
    for (var k = 1; k <= n; k++) {
      var sub = prev[k - 1] + (needle.charCodeAt(k - 1) === hc ? 0 : 1);
      var del = prev[k] + 1;
      var ins = curr[k - 1] + 1;
      curr[k] = Math.min(sub, del, ins);
    }
    var last = curr[n];
    if (last < best) best = last;
    var tmp = prev;
    prev = curr;
    curr = tmp;
  }

  return best;
}

/**
 * Fuzzy substring search — checks if `needle` approximately exists in
 * `haystack`. Returns the best needle-normalized similarity found, or 0 if
 * below `minScore`. Pipeline (cheapest first): exact containment → direct
 * comparison when the needle is longer than the haystack → Sellers approximate
 * substring search. Callers are expected to pass `normalizeText`-processed
 * strings; this function compares verbatim.
 */
function fuzzyIncludes(haystack, needle, minScore) {
  var threshold = minScore === undefined ? 0.6 : minScore;
  if (!needle || !haystack) return 0;
  if (haystack.includes(needle)) return 1;

  var capped = haystack.length > HAYSTACK_CAP ? haystack.slice(0, HAYSTACK_CAP) : haystack;

  if (needle.length > capped.length) {
    var score = similarity(capped, needle);
    return score >= threshold ? score : 0;
  }

  if (needle.length < MIN_FUZZY_NEEDLE_LENGTH && capped.length > SHORT_NEEDLE_HAYSTACK_CAP) {
    return 0;
  }

  var sellers = 1 - sellersDistance(capped, needle) / needle.length;
  return sellers >= threshold ? sellers : 0;
}

/**
 * Character-bigram multiset of a string, keyed by packed char-code pairs.
 * Built once per resolution for the stored snippet, then streamed against
 * every scan candidate via `diceAgainst` — no per-candidate copy.
 */
function bigramCounts(s) {
  var counts = new Map();
  for (var i = 0; i < s.length - 1; i++) {
    var key = (s.charCodeAt(i) << 16) | s.charCodeAt(i + 1);
    counts.set(key, (counts.get(key) || 0) + 1);
  }
  return counts;
}

/**
 * Sørensen–Dice coefficient between a precomputed bigram multiset and a raw
 * string, in O(|text|). Used exclusively as a RANKING signal for scan
 * prefiltering, never as an eliminating threshold.
 */
function diceAgainst(needleCounts, needleBigramTotal, text) {
  var textBigramTotal = text.length - 1;
  if (needleBigramTotal <= 0 || textBigramTotal <= 0) return 0;

  var matches = 0;
  var consumed = new Map();
  for (var i = 0; i < text.length - 1; i++) {
    var key = (text.charCodeAt(i) << 16) | text.charCodeAt(i + 1);
    var available = needleCounts.get(key);
    if (available === undefined) continue;
    var used = consumed.get(key) || 0;
    if (used < available) {
      consumed.set(key, used + 1);
      matches++;
    }
  }

  return (2 * matches) / (needleBigramTotal + textBigramTotal);
}

/**
 * Word-pair shingle multiset ("quick brown", "brown fox", …) of a
 * whitespace-normalized string. Character bigrams are order-blind; word pair
 * counts restore order sensitivity.
 */
function wordPairCounts(s) {
  var counts = new Map();
  var words = s.split(" ");
  for (var i = 0; i < words.length - 1; i++) {
    var key = words[i] + " " + words[i + 1];
    counts.set(key, (counts.get(key) || 0) + 1);
  }
  return counts;
}

/** Sørensen–Dice over word-pair shingles, multiset semantics, O(|text|). */
function wordPairDiceAgainst(needlePairs, needlePairTotal, text) {
  if (needlePairTotal <= 0) return 0;
  var words = text.split(" ");
  var textPairTotal = words.length - 1;
  if (textPairTotal <= 0) return 0;

  var matches = 0;
  var consumed = new Map();
  for (var i = 0; i < words.length - 1; i++) {
    var key = words[i] + " " + words[i + 1];
    var available = needlePairs.get(key);
    if (available === undefined) continue;
    var used = consumed.get(key) || 0;
    if (used < available) {
      consumed.set(key, used + 1);
      matches++;
    }
  }

  return (2 * matches) / (needlePairTotal + textPairTotal);
}

// ---------------------------------------------------------------------------
// visibility.ts
// ---------------------------------------------------------------------------

/**
 * Both option-name generations must be passed together: the CSSOM spec ORs
 * the old and renamed flags, and older Chrome/Firefox silently ignore the new
 * names. `contentVisibilityAuto` stays OFF — `content-visibility: auto`
 * content is merely offscreen, a legitimate anchor target.
 */
var STRICT_VISIBILITY_OPTIONS = {
  checkOpacity: true,
  opacityProperty: true,
  checkVisibilityCSS: true,
  visibilityProperty: true,
};

/**
 * Classify how visible an element currently is. Visibility is a scoring TIER,
 * never a hard filter — when every duplicate is hidden, the best hidden
 * candidate still resolves rather than orphaning the annotation.
 */
function classifyVisibility(element) {
  if (!element.isConnected) return "hidden";

  if (typeof element.checkVisibility === "function") {
    if (element.checkVisibility(STRICT_VISIBILITY_OPTIONS)) return "visible";
    return element.checkVisibility() ? "soft-hidden" : "hidden";
  }

  if (element.getClientRects().length > 0) {
    try {
      var visibility = getComputedStyle(element).visibility;
      return visibility === "visible" ? "visible" : "soft-hidden";
    } catch {
      return "visible";
    }
  }

  // display:contents generates no box of its own but renders its children.
  var child = element.firstElementChild;
  if (child && child.getClientRects().length > 0) return "visible";

  // No boxes anywhere — hidden, unless the environment does no layout at all
  // (then every element has zero rects and the signal is meaningless).
  if (element.ownerDocument.documentElement.getClientRects().length === 0) return "unknown";

  return "hidden";
}

/** Score multiplier for a visibility class — tiering, not filtering. */
function visibilityFactor(cls) {
  switch (cls) {
    case "hidden":
      return 0.3;
    case "soft-hidden":
      return 0.6;
    default:
      return 1;
  }
}

// ---------------------------------------------------------------------------
// resolver.ts
// ---------------------------------------------------------------------------

/**
 * Confidence ceiling per strategy, used as a multiplicative prior in
 * cross-strategy ranking. Multiplicative, not additive: a prior can only
 * amplify verification evidence, never substitute for it.
 */
var STRATEGY_PRIORS = { id: 1.0, css: 0.95, xpath: 0.9, scan: 0.85 };

/** Max matches gathered per selector strategy (guards degenerate selectors). */
var MAX_PER_STRATEGY = 16;

/** Scan candidates that survive prefiltering and get full multi-signal scoring. */
var SCAN_TOP_K = 24;

/** Pathological-DOM guard for the prefilter sweep itself. */
var SCAN_HARD_CAP = 10000;

/** Max candidate text considered for scoring (bounds fuzzy-match cost). */
var CANDIDATE_TEXT_CAP = 500;

/**
 * Acceptance for selector strategies gates on the STRONGEST single signal,
 * never on the diluted blend — the blend is a RANKING device. A volatile
 * signal (fingerprints churn with every redesign) must not veto a stable one.
 * - text ≥ 0.5 (fuzzyIncludes' own minScore) accepts.
 * - No stored text at all: ALWAYS acceptable — textless selector hits, such
 *   as icon/image anchors, must not be silently orphaned.
 * - Text present but REFUTED: strong structural corroboration required — the
 *   i18n case (text translated, fingerprint intact) passes, a weak
 *   fingerprint coincidence does not.
 * Scan has no selector evidence, so it keeps a 0.4 floor on the blend.
 */
var TEXT_FLOOR = 0.5;
var STRONG_STRUCT = 0.6;
var ACCEPT_SCAN = 0.4;

/** Hostile-stored-data guard: fields are sliced before any normalization
 * (NFC on a multi-megabyte string is itself a freeze) and again after. */
var RAW_FIELD_CAP = 2000;

/**
 * Verification at or above this level earns the full strategy prior as
 * confidence; below it, confidence degrades proportionally.
 */
var STRONG_VERIFY = 0.8;

/**
 * Verification assigned to selector-strategy candidates when the stored
 * anchor carries no verifiable signal at all. The selector match is then the
 * only evidence: accept, but rank it below verified alternatives.
 */
var NEUTRAL_VERIFICATION = 0.6;

/** Score gap under which two candidates are considered equally plausible. */
var AMBIGUITY_EPSILON = 0.05;

function boundedField(value, cap) {
  return normalizeText((typeof value === "string" ? value : "").slice(0, RAW_FIELD_CAP)).slice(
    0,
    cap,
  );
}

function buildSignals(anchor) {
  // Capture stores ≤200-char snippets and ≤32-char context — larger values
  // only come from hostile or corrupted storage, and uncapped they turn
  // normalization/edit-distance into a main-thread freeze.
  var quote = anchor.quote || {};
  var snippet = boundedField(quote.exact, CANDIDATE_TEXT_CAP);
  var snippetWordPairs = wordPairCounts(snippet);
  var snippetWordPairTotal = 0;
  snippetWordPairs.forEach(function (count) {
    snippetWordPairTotal += count;
  });
  return {
    snippet: snippet,
    snippetBigrams: bigramCounts(snippet),
    snippetBigramTotal: Math.max(0, snippet.length - 1),
    snippetWordPairs: snippetWordPairs,
    snippetWordPairTotal: snippetWordPairTotal,
    prefix: boundedField(quote.prefix, 128),
    suffix: boundedField(quote.suffix, 128),
    neighbor: boundedField(anchor.neighborText, 128),
    fingerprint: (anchor.fingerprint || "").slice(0, 64),
    tag: typeof anchor.tag === "string" ? anchor.tag : "",
  };
}

/** A tree selector strategies can be queried against. */
function queryRoots(roots, selector) {
  var found = [];
  for (var r = 0; r < roots.length; r++) {
    var matches = roots[r].querySelectorAll(selector);
    for (var i = 0; i < matches.length && found.length < MAX_PER_STRATEGY; i++) {
      var el = matches[i];
      if (el) found.push(el);
    }
    if (found.length === MAX_PER_STRATEGY) break;
  }
  return found;
}

/**
 * Follow a shadow-captured selector's host segments down from the document:
 * `levels[i]` holds the open shadow roots of the hosts matched by segment i,
 * the last level being the anchored element's own tree. Each segment is capped
 * at MAX_PER_STRATEGY hosts. Returns [] when the chain breaks (host gone,
 * renamed, or its root closed).
 */
function resolveShadowLevels(hostSelectors) {
  var levels = [];
  var roots = [document];
  for (var s = 0; s < hostSelectors.length; s++) {
    var hosts;
    try {
      hosts = queryRoots(roots, hostSelectors[s]);
    } catch {
      return [];
    }
    var next = [];
    for (var h = 0; h < hosts.length; h++) {
      if (hosts[h].shadowRoot) next.push(hosts[h].shadowRoot);
    }
    if (next.length === 0) return [];
    levels.push(next);
    roots = next;
  }
  return levels;
}

/**
 * Collect candidates from all selector strategies, ALL matches per strategy
 * (bounded), in priority order. An element found by several strategies keeps
 * the highest-priority one (first insertion wins).
 *
 * A shadow-captured selector (`host >>> inner`) cannot match at document
 * level by construction, so that anchor descends into the open roots its host
 * chain leads to. Ids and selectors are tree-scoped; the light-DOM case runs
 * against the document exactly as before.
 */
function gatherSelectorCandidates(anchor) {
  var pool = new Map();

  function add(el, strategy, enforceTag) {
    // Our overlay chrome must never be a resolution target, whichever selector
    // strategy found it; anything inside the host or shield is ours too.
    if (!el || pool.has(el) || isOverlayChrome(el)) return;
    if (enforceTag && el.tagName !== anchor.tag) return;
    pool.set(el, strategy);
  }

  var segments = typeof anchor.css === "string" ? anchor.css.split(SHADOW_BOUNDARY) : [];
  var selector = segments.length > 0 ? segments[segments.length - 1] : anchor.css;
  var shadowLevels = segments.length > 1 ? resolveShadowLevels(segments.slice(0, -1)) : null;
  var treeRoots = shadowLevels ? shadowLevels[shadowLevels.length - 1] || [] : [document];

  // id — duplicate ids are invalid HTML but common in the wild; gather all so
  // a hidden duplicate can lose to the visible one instead of shadowing it.
  // Attribute selector instead of `#…` to avoid needing CSS.escape (absent in
  // some embedders and in jsdom) — matching semantics are identical.
  if (anchor.id) {
    var escapedId = anchor.id.replace(/\\/g, "\\\\").replace(/"/g, '\\"');
    try {
      var idMatches = queryRoots(treeRoots, '[id="' + escapedId + '"]');
      for (var i = 0; i < idMatches.length; i++) add(idMatches[i], "id", true);
    } catch {
      for (var r = 0; r < treeRoots.length; r++)
        add(treeRoots[r].getElementById(anchor.id), "id", true);
    }
  }

  // CSS selector — its innermost segment, for a shadow anchor
  try {
    var cssMatches = queryRoots(treeRoots, selector);
    for (var c = 0; c < cssMatches.length; c++) add(cssMatches[c], "css", true);
  } catch {
    // Invalid selector — skip strategy
  }

  // XPath cannot enter shadow trees: a shadow anchor's path is informational.
  if (shadowLevels) return { pool: pool, treeRoots: treeRoots };

  // XPath — snapshot (not FIRST_ORDERED_NODE) so later duplicates compete too
  try {
    var result = document.evaluate(
      anchor.xpath,
      document,
      null,
      XPathResult.ORDERED_NODE_SNAPSHOT_TYPE,
      null,
    );
    var count = Math.min(result.snapshotLength, MAX_PER_STRATEGY);
    for (var x = 0; x < count; x++) {
      var node = result.snapshotItem(x);
      if (node instanceof Element) add(node, "xpath", true);
    }
  } catch {
    // Invalid XPath — skip strategy
  }

  return { pool: pool, treeRoots: treeRoots };
}

/**
 * Same-tag sweep, prefiltered. Every same-tag element is considered, but full
 * scoring only runs on the TOP_K best by a cheap O(text-budget) prefilter:
 * bigram-Dice text overlap plus O(1) structural hints. The prefilter only
 * RANKS — it never eliminates on a threshold, so an imperfect cheap score
 * demotes a candidate but cannot drop the true match on its own.
 */
function sweepScanCandidates(signals, pool, roots) {
  var tag = signals.tag.toLowerCase();
  if (!tag) return [];
  var candidates = [];
  try {
    for (var r = 0; r < roots.length; r++) {
      var matches = roots[r].querySelectorAll(tag);
      for (var i = 0; i < matches.length && candidates.length < SCAN_HARD_CAP; i++) {
        var el = matches[i];
        if (el) candidates.push(el);
      }
      if (candidates.length === SCAN_HARD_CAP) break;
    }
  } catch {
    return [];
  }

  // Parse only well-formed 3-part fingerprints (mirrors scoreFingerprint).
  // `Number("")` is 0, not NaN — an empty fingerprint would otherwise grant a
  // phantom child-count-0 bonus that biases top-K toward childless decoys.
  var storedFp = signals.fingerprint.split(":");
  var storedChildCount = storedFp.length === 3 ? Number(storedFp[0]) : Number.NaN;
  var storedAttrHash = storedFp.length === 3 ? storedFp[2] : "";

  var ranked = [];

  for (var c = 0; c < candidates.length; c++) {
    var candidate = candidates[c];
    // Skip pool members (already scored under a selector strategy) and our own
    // overlay chrome, whose text/fingerprint would otherwise lure the fallback.
    if (pool.has(candidate) || isOverlayChrome(candidate)) continue;

    var cheap = 0;
    if (signals.snippetBigramTotal > 0) {
      // collapseWhitespace, not full normalizeText: NFC on every candidate is
      // the prefilter's dominant hidden cost; survivors get re-normalized.
      var text = collapseWhitespace(boundedText(candidate, CANDIDATE_TEXT_CAP));
      var charDice = diceAgainst(signals.snippetBigrams, signals.snippetBigramTotal, text);
      // Character bigrams are order-blind: on shared-vocabulary pages every
      // candidate scores alike. Word-pair shingles restore order sensitivity.
      if (signals.snippetWordPairTotal > 0) {
        var wordDice =
          charDice > 0
            ? wordPairDiceAgainst(signals.snippetWordPairs, signals.snippetWordPairTotal, text)
            : 0;
        cheap += 0.6 * (0.5 * charDice + 0.5 * wordDice);
      } else {
        cheap += 0.6 * charDice;
      }
    }
    if (storedAttrHash && attrHash(candidate) === storedAttrHash) cheap += 0.25;
    if (!Number.isNaN(storedChildCount)) {
      var diff = Math.abs(candidate.children.length - storedChildCount);
      if (diff === 0) cheap += 0.15;
      else if (diff <= 2) cheap += 0.07;
    }

    ranked.push({ element: candidate, cheap: cheap });
  }

  // Stable sort: ties keep document order.
  ranked.sort(function (a, b) {
    return b.cheap - a.cheap;
  });
  return ranked.slice(0, SCAN_TOP_K).map(function (entry) {
    return entry.element;
  });
}

function scoreOne(element, strategy, signals) {
  var scores = verificationScore(element, signals);
  var visibility = visibilityFactor(classifyVisibility(element));
  if (scores === null) {
    // No verifiable signal stored: the selector match is the only evidence.
    // A scan candidate with nothing to verify against is meaningless, though.
    if (strategy === "scan") return null;
    return {
      element: element,
      strategy: strategy,
      verification: NEUTRAL_VERIFICATION,
      strongest: NEUTRAL_VERIFICATION,
      visibility: visibility,
      final: STRATEGY_PRIORS[strategy] * NEUTRAL_VERIFICATION * visibility,
      unverified: true,
    };
  }

  return {
    element: element,
    strategy: strategy,
    verification: scores.blend,
    strongest: scores.strongest,
    signals: scores,
    visibility: visibility,
    final: STRATEGY_PRIORS[strategy] * scores.blend * visibility,
  };
}

/**
 * Whether a candidate has enough corroboration to be returned at all.
 * Selector strategies accept on the strongest SINGLE signal clearing its
 * floor; scan keeps its blend floor.
 */
function isAcceptable(c) {
  if (c.strategy === "scan") return c.verification >= ACCEPT_SCAN;
  if (c.unverified || !c.signals) return true;
  var s = c.signals;
  // No stored text: ranking-only — textless selector hits must not be orphaned.
  if (s.text === undefined) return true;
  if (s.text >= TEXT_FLOOR) return true;
  // Text present but refuted — accept only on strong structural agreement.
  return (
    (s.fingerprint || 0) >= STRONG_STRUCT ||
    (s.context || 0) >= STRONG_STRUCT ||
    (s.neighbor || 0) >= STRONG_STRUCT
  );
}

/**
 * Multi-signal verification, 0–1. Dynamic weighting: only signals the stored
 * anchor actually has contribute, then the sum is normalized. Returns null
 * when the anchor stores no verifiable signal at all.
 */
function verificationScore(candidate, s) {
  var score = 0;
  var totalWeight = 0;
  var out = { blend: 0, strongest: 0 };

  // --- Text snippet (weight 40 — most reliable under reordering) ---
  if (s.snippet) {
    totalWeight += 40;
    var candidateText = normalizeText(boundedText(candidate, CANDIDATE_TEXT_CAP));
    out.text = fuzzyIncludes(candidateText, s.snippet, 0.5);
    score += out.text * 40;
  }

  // --- Fingerprint (weight 20) ---
  if (s.fingerprint) {
    totalWeight += 20;
    out.fingerprint = scoreFingerprint(candidate, s.fingerprint);
    score += out.fingerprint * 20;
  }

  // --- Prefix/suffix context (weight 20) ---
  if (s.prefix || s.suffix) {
    totalWeight += 20;
    var contextScore = 0;
    var contextParts = 0;

    if (s.prefix) {
      var prevText = normalizeText(adjacentText(candidate, "before"));
      contextScore += prevText ? similarity(prevText, s.prefix) : 0;
      contextParts++;
    }

    if (s.suffix) {
      var nextText = normalizeText(adjacentText(candidate, "after"));
      contextScore += nextText ? similarity(nextText, s.suffix) : 0;
      contextParts++;
    }

    if (contextParts > 0) {
      out.context = contextScore / contextParts;
      score += out.context * 20;
    }
  }

  // --- Neighbor text (weight 20) ---
  if (s.neighbor) {
    totalWeight += 20;
    var candidateNeighbor = normalizeText(neighborText(candidate));
    out.neighbor = candidateNeighbor ? similarity(candidateNeighbor, s.neighbor) : 0;
    score += out.neighbor * 20;
  }

  if (totalWeight === 0) return null;
  out.blend = score / totalWeight;
  out.strongest = Math.max(
    out.text || 0,
    out.fingerprint || 0,
    out.context || 0,
    out.neighbor || 0,
  );
  return out;
}

/**
 * Ancestor-decoy tie-break: a wrapper's text contains its child's text, so it
 * scores nearly as well — but anchoring to the wrapper corrupts the rect.
 * Among candidates within AMBIGUITY_EPSILON of the best that sit on the
 * best's own descendant chain, prefer the innermost. Unrelated near-ties keep
 * the plain argmax (strategy priors already encode the preference order).
 */
function disambiguate(sortedAccepted) {
  var best = sortedAccepted[0];
  var contenders = sortedAccepted.filter(function (c) {
    return (
      best.final - c.final <= AMBIGUITY_EPSILON && (c === best || best.element.contains(c.element))
    );
  });
  var winner = best;
  for (var i = 0; i < contenders.length; i++) {
    var c = contenders[i];
    if (c !== winner && winner.element.contains(c.element)) winner = c;
  }
  return winner;
}

function confidenceOf(c) {
  if (c.strategy === "scan") {
    // The scan's confidence IS its verification, hard-capped — a scan is
    // never fully certain.
    return Math.min(c.verification, STRATEGY_PRIORS.scan);
  }
  // Nothing was verifiable → the selector is all the evidence there is;
  // trust it at full prior rather than inventing doubt.
  if (c.unverified) return STRATEGY_PRIORS[c.strategy];
  // Confidence follows the STRONGEST corroborating signal, not the blend:
  // absence of a signal stays neutral; a present-but-refuted one still drags
  // `strongest` down when it is all there is. Full prior at STRONG_VERIFY.
  return STRATEGY_PRIORS[c.strategy] * Math.min(1, c.strongest / STRONG_VERIFY);
}

/**
 * Re-anchor an annotation: gather candidates from EVERY strategy, verify each
 * against all stored signals, rank across strategies, return the best.
 *
 * final = strategyPrior × verification × visibilityFactor
 * - visibility: tiering, never filtering — hidden duplicates are heavily
 *   penalized, but when ONLY hidden candidates exist the best one still
 *   resolves (an annotation on a currently-collapsed section survives the
 *   breakpoint flipping back).
 *
 * The scan sweep is skipped when a selector candidate already scores above
 * anything the scan could produce (its prior caps its final at 0.85) — the
 * happy path costs a handful of querySelector calls.
 *
 * Returns null if no candidate verifies (annotation is orphaned).
 */
function resolveAnchor(anchor) {
  var signals = buildSignals(anchor);
  var gathered = gatherSelectorCandidates(anchor);
  var pool = gathered.pool;
  var treeRoots = gathered.treeRoots;

  var scored = [];
  pool.forEach(function (strategy, element) {
    var candidate = scoreOne(element, strategy, signals);
    if (candidate) scored.push(candidate);
  });

  var bestFinal = 0;
  var strongVisibleMatch = false;
  for (var i = 0; i < scored.length; i++) {
    var c = scored[i];
    if (c.final > bestFinal) bestFinal = c.final;
    // Identity selectors (id — near-unique by contract, the ones trusted
    // absolutely) with near-exact text on a visible element end the search
    // even when CONTEXT signals drifted — but not when the element's own
    // fingerprint contradicts. Fragile selectors (css/xpath) get no shortcut:
    // an element that merely CONTAINS the snippet verbatim plus coincidental
    // structure is the impostor case.
    if (
      c.strategy === "id" &&
      c.visibility === 1 &&
      (c.signals && c.signals.text ? c.signals.text : 0) >= STRONG_VERIFY &&
      (c.signals.fingerprint === undefined || c.signals.fingerprint >= STRONG_VERIFY)
    ) {
      strongVisibleMatch = true;
    }
  }

  // The sweep only helps when something could beat the pool (scan's final is
  // capped by its prior) AND there is at least one stored signal to verify
  // scan candidates against — with none, every scan candidate is discarded
  // and the sweep is pure cost. A shadow anchor whose host chain no longer
  // resolves has no tree to sweep at all.
  var verifiable = !!(
    signals.snippet ||
    signals.fingerprint ||
    signals.prefix ||
    signals.suffix ||
    signals.neighbor
  );
  if (
    verifiable &&
    treeRoots.length > 0 &&
    bestFinal < STRATEGY_PRIORS.scan &&
    !strongVisibleMatch
  ) {
    var swept = sweepScanCandidates(signals, pool, treeRoots);
    for (var s = 0; s < swept.length; s++) {
      var candidate = scoreOne(swept[s], "scan", signals);
      if (candidate) scored.push(candidate);
    }
  }

  var accepted = scored.filter(isAcceptable);
  if (accepted.length === 0) return null;

  accepted.sort(function (a, b) {
    return b.final - a.final;
  });
  var winner = disambiguate(accepted);

  return {
    element: winner.element,
    confidence: confidenceOf(winner),
    strategy: winner.strategy,
  };
}

/** Resolve an anchor target to its current element, or null when orphaned. */
ns.resolveTarget = function (target) {
  var resolution = resolveAnchor(target || {});
  return resolution ? resolution.element : null;
};
