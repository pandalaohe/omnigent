"""Count, visibility, clipping and readable text at the usage-board boundary."""

from playwright.sync_api import Page

USAGE_GEOMETRY = """expected => {
  const scrollPositions = new Map();
  const windowPosition = {left: window.scrollX, top: window.scrollY};
  try {
  const failures = [];
  const counts = Object.entries(expected ?? {});
  if (!counts.some(([, count]) => count > 0))
    failures.push('expected element counts must include a non-zero view');
  const visible = el => el.checkVisibility({checkOpacity: true, checkVisibilityCSS: true}) &&
    el.getBoundingClientRect().width > 0 && el.getBoundingClientRect().height > 0;
  const inside = (outer, inner) => inner.left >= outer.left - 1 &&
    inner.right <= outer.right + 1 && inner.top >= outer.top - 1 &&
    inner.bottom <= outer.bottom + 1;
  const describe = el => `${el.tagName}.${el.className?.baseVal ?? el.className}: ` +
    el.textContent?.trim().slice(0, 80);
  // Only the Models table deliberately scrolls horizontally.
  const horizontalScrollRegions = '.usage-board .overflow-x-auto:has(> table)';
  const verticalScroller = style => ['auto', 'scroll'].includes(style.overflowY);
  const horizontalScroller = (el, style) => el.matches(horizontalScrollRegions) &&
    ['auto', 'scroll'].includes(style.overflowX);
  const reveal = el => {
    for (let parent = el.parentElement; parent; parent = parent.parentElement) {
      const style = getComputedStyle(parent), box = parent.getBoundingClientRect();
      if (!verticalScroller(style) && !horizontalScroller(parent, style)) continue;
      if (!scrollPositions.has(parent))
        scrollPositions.set(parent, {left: parent.scrollLeft, top: parent.scrollTop});
      const rect = el.getBoundingClientRect();
      let visibleTop = rect.top, visibleBottom = rect.bottom;
      if (!el.children.length && el.textContent?.trim() &&
          getComputedStyle(el).overflowY === 'visible') {
        const range = document.createRange();
        range.selectNodeContents(el);
        const ink = range.getBoundingClientRect();
        visibleTop = Math.min(visibleTop, ink.top);
        visibleBottom = Math.max(visibleBottom, ink.bottom);
      }
      const left = box.left + parent.clientLeft, top = box.top + parent.clientTop;
      const shift = (start, end, near, size) => start < near || end - start > size
        ? start - near : Math.max(0, end - near - size);
      parent.scrollTo({
        top: parent.scrollTop + (verticalScroller(style)
          ? shift(visibleTop, visibleBottom, top, parent.clientHeight) : 0),
        left: parent.scrollLeft + (horizontalScroller(parent, style)
          ? shift(rect.left, rect.right, left, parent.clientWidth) : 0),
        behavior: 'instant',
      });
    }
  };
  const scrollProjection = (parent, rect) => {
    const style = getComputedStyle(parent), box = parent.getBoundingClientRect();
    const left = box.left + parent.clientLeft, top = box.top + parent.clientTop;
    const clamp = (value, near, size) => Math.min(near + size, Math.max(near, value));
    return {
      left: horizontalScroller(parent, style)
        ? clamp(rect.left, left, parent.clientWidth) : rect.left,
      right: horizontalScroller(parent, style)
        ? clamp(rect.right, left, parent.clientWidth) : rect.right,
      top: verticalScroller(style) ? clamp(rect.top, top, parent.clientHeight) : rect.top,
      bottom: verticalScroller(style) ? clamp(rect.bottom, top, parent.clientHeight) : rect.bottom,
    };
  };
  const within = (bound, rect, el) => {
    for (let parent = el.parentElement; parent && parent !== bound; parent = parent.parentElement)
      rect = scrollProjection(parent, rect);
    return inside(bound.getBoundingClientRect(), rect);
  };
  const clipping = (el, rect, ink = false) => {
    const ring = el.closest('[data-testid="usage-ring"], .usage-balance-ring');
    const clips = value => ['hidden', 'clip', 'auto', 'scroll'].includes(value);
    for (let parent = ink ? el : el.parentElement; parent; parent = parent.parentElement) {
      const style = getComputedStyle(parent);
      const box = parent.getBoundingClientRect();
      const left = box.left + parent.clientLeft, top = box.top + parent.clientTop;
      const clipPath = style.clipPath !== 'none';
      if ((clipPath || (clips(style.overflowX) &&
          (ring || !horizontalScroller(parent, style)))) &&
          (rect.left < left - 1 || rect.right > left + parent.clientWidth + 1))
        failures.push(`ancestor clips horizontally: ${describe(el)}`);
      if ((clipPath || (clips(style.overflowY) && (ring || !verticalScroller(style)))) &&
          (rect.top < top - 1 || rect.bottom > top + parent.clientHeight + 1))
        failures.push(`ancestor clips vertically: ${describe(el)}`);
      if (!ring) rect = scrollProjection(parent, rect);
      if (parent === document.documentElement) break;
    }
  };
  const requireVisible = el => {
    reveal(el);
    if (!visible(el)) {
      failures.push(`not visible or zero size: ${describe(el)}`);
      return false;
    }
    clipping(el, el.getBoundingClientRect());
    return true;
  };
  for (const [selector, count] of counts) {
    const elements = document.querySelectorAll(selector);
    if (elements.length !== count)
      failures.push(`expected ${count} ${selector}, found ${elements.length}`);
    for (const el of elements) requireVisible(el);
  }
  const figures = '[data-testid="usage-ring"], .usage-balance-ring, [data-usage-amount], ' +
    '[data-usage-label], [data-usage-percent]';
  const textRects = el => {
    const range = document.createRange(), rects = [];
    const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      if (!node.textContent.trim()) continue;
      range.selectNodeContents(node);
      rects.push(...range.getClientRects());
    }
    return rects;
  };
  const ellipsized = el => getComputedStyle(el).textOverflow === 'ellipsis' &&
    el.title === el.textContent.trim();
  for (const el of document.querySelectorAll('[data-usage-name]')) {
    if (!visible(el)) continue;
    reveal(el);
    const style = getComputedStyle(el), box = el.getBoundingClientRect();
    if (el.title !== el.textContent)
      failures.push(`missing full name title: ${describe(el)}`);
    if (style.textOverflow !== 'ellipsis' || style.whiteSpace !== 'nowrap' ||
        !['hidden', 'clip'].includes(style.overflowX))
      failures.push(`unbounded name: ${describe(el)}`);
    const card = el.closest('[data-testid="usage-tile"], [data-usage-window], ' +
      '[data-testid="usage-tip-card"], .ua-panel, .usage-board');
    if (card && !within(card, box, el))
      failures.push(`name outside card: ${describe(el)}`);
    clipping(el, box);
  }
  for (const el of document.querySelectorAll(figures)) {
    if (!requireVisible(el)) continue;
    const card = el.closest('.usage-unit, [data-usage-window], ' +
      '[data-testid="usage-tile"], [data-testid="usage-balances"]');
    if (card && !within(card, el.getBoundingClientRect(), el))
      failures.push(`figure outside tile: ${describe(el)}`);
    const tile = el.closest('[data-testid="usage-tile"]');
    if (tile && !within(tile, el.getBoundingClientRect(), el))
      failures.push(`figure outside account: ${describe(el)}`);
    if (el.matches('[data-usage-amount], [data-usage-label], [data-usage-percent]')) {
      if (parseFloat(getComputedStyle(el).fontSize) < 10)
        failures.push(`text below 10px: ${describe(el)}`);
      const ellipsis = ellipsized(el);
      const rects = ellipsis ? [el.getBoundingClientRect()] : textRects(el);
      for (const rect of rects) {
        if (card && !within(card, rect, el))
          failures.push(`text outside tile: ${describe(el)}`);
        if (tile && !within(tile, rect, el))
          failures.push(`text outside account: ${describe(el)}`);
        clipping(el, rect, !ellipsis);
      }
      if (el.matches('[data-usage-percent]')) {
        const small = el.closest('[data-testid="usage-ring"]').getBoundingClientRect().width < 64;
        const reading = el.textContent.trim();
        const pattern = small ? /^[<>]?\\d+(?:\\.\\d{1,2})?$/
          : /^[<>]?\\d+(?:\\.\\d{1,2})?%$/;
        if (!pattern.test(reading))
          failures.push(`split percentage: ${describe(el)}`);
        // A stacked reading (whole above decimals) keeps each of its lines unbroken.
        const lines = el.dataset.usagePercent === 'stack'
          ? [...el.children].map(textRects) : [rects];
        for (const line of lines) {
          const height = Math.max(...line.map(rect => rect.height));
          if (Math.max(...line.map(rect => rect.bottom)) -
              Math.min(...line.map(rect => rect.bottom)) > height * 0.6)
            failures.push(`wrapped percentage: ${describe(el)}`);
        }
      }
    }
  }
  for (const ring of document.querySelectorAll('[data-testid="usage-ring"]')) {
    if (!visible(ring)) continue;
    reveal(ring);
    const bounds = ring.getBoundingClientRect();
    const inner = bounds.width * 0.375;
    for (const text of ring.querySelectorAll('.usage-ring-name, [data-usage-percent]')) {
      const rects = ellipsized(text) ? [text.getBoundingClientRect()] : textRects(text);
      for (const rect of rects) for (const x of [rect.left, rect.right])
        for (const y of [rect.top + rect.height * 0.18, rect.bottom - rect.height * 0.18]) {
          const clearance = inner - Math.hypot(x - bounds.left - bounds.width / 2,
            y - bounds.top - bounds.height / 2);
          if (clearance < (bounds.width >= 64 ? 2 : 1))
            failures.push(`text crowds ring: ${describe(text)} (${clearance.toFixed(2)}px)`);
        }
    }
    for (const circle of ring.querySelectorAll('circle')) {
      const box = circle.getBoundingClientRect(), matrix = circle.getScreenCTM();
      const stroke = parseFloat(getComputedStyle(circle).strokeWidth) / 2;
      const dx = stroke * Math.hypot(matrix.a, matrix.c);
      const dy = stroke * Math.hypot(matrix.b, matrix.d);
      const painted = {left: box.left - dx, right: box.right + dx,
        top: box.top - dy, bottom: box.bottom + dy};
      if (!inside(ring.getBoundingClientRect(), painted))
        failures.push(`arc outside ring: ${describe(ring)}`);
      clipping(circle, painted);
    }
  }
  const texts = '.usage-board :is(p, b, strong, label, button, a, small, span)';
  for (const el of document.querySelectorAll(texts)) {
    if (!visible(el) || el.children.length || !el.textContent.trim() ||
        el.closest('[data-testid="usage-tip-card"]')) continue;
    reveal(el);
    const style = getComputedStyle(el);
    if (parseFloat(style.fontSize) < 10) failures.push(`text below 10px: ${describe(el)}`);
    const card = el.closest('.usage-unit, [data-testid="usage-tile"], ' +
      '.ua-panel, .ua-kpi, .usage-pricing, .usage-board');
    const ellipsis = ellipsized(el);
    for (const rect of ellipsis ? [el.getBoundingClientRect()] : textRects(el)) {
      if (card && !within(card, rect, el))
        failures.push(`text outside card: ${describe(el)}`);
      clipping(el, rect, !ellipsis);
    }
  }
  if (document.documentElement.scrollWidth > innerWidth) failures.push('document overflow');
  return [...new Set(failures)];
  } finally {
    for (const [container, position] of [...scrollPositions].reverse())
      container.scrollTo({...position, behavior: 'instant'});
    window.scrollTo({...windowPosition, behavior: 'instant'});
  }
}"""


def assert_usage_geometry(page: Page, expected: dict[str, int]) -> None:
    page.evaluate("() => document.fonts.ready")
    failures = page.evaluate(USAGE_GEOMETRY, expected)
    assert failures == [], (page.url, failures)
