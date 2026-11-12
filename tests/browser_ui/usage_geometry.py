"""Count, visibility, clipping and readable text at the usage-board boundary."""

from playwright.sync_api import Page

USAGE_GEOMETRY = """expected => {
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
  const clipping = (el, rect, ink = false) => {
    const tile = el.closest('[data-testid="usage-tile"]') || el.closest(
      '[data-usage-window], [data-testid="usage-balances"], .ua-panel, .ua-kpi, ' +
      '.usage-pricing, .usage-board');
    if (el === tile) return;
    for (let parent = ink ? el : el.parentElement; parent; parent = parent.parentElement) {
      const style = getComputedStyle(parent);
      const box = parent.getBoundingClientRect();
      const left = box.left + parent.clientLeft, top = box.top + parent.clientTop;
      if (style.overflowX !== 'visible' &&
          (rect.left < left - 1 || rect.right > left + parent.clientWidth + 1))
        failures.push(`ancestor clips horizontally: ${describe(el)}`);
      if (style.overflowY !== 'visible' &&
          (rect.top < top - 1 || rect.bottom > top + parent.clientHeight + 1))
        failures.push(`ancestor clips vertically: ${describe(el)}`);
      if (parent === tile) break;
    }
  };
  const requireVisible = el => {
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
  for (const el of document.querySelectorAll(figures)) {
    if (!requireVisible(el)) continue;
    const card = el.closest('.usage-unit, [data-usage-window], ' +
      '[data-testid="usage-tile"], [data-testid="usage-balances"]');
    if (card && !inside(card.getBoundingClientRect(), el.getBoundingClientRect()))
      failures.push(`figure outside tile: ${describe(el)}`);
    const tile = el.closest('[data-testid="usage-tile"]');
    if (tile && !inside(tile.getBoundingClientRect(), el.getBoundingClientRect()))
      failures.push(`figure outside account: ${describe(el)}`);
    if (el.matches('[data-usage-amount], [data-usage-label], [data-usage-percent]')) {
      if (parseFloat(getComputedStyle(el).fontSize) < 10)
        failures.push(`text below 10px: ${describe(el)}`);
      const rects = textRects(el);
      for (const rect of rects) {
        if (card && !inside(card.getBoundingClientRect(), rect))
          failures.push(`text outside tile: ${describe(el)}`);
        if (tile && !inside(tile.getBoundingClientRect(), rect))
          failures.push(`text outside account: ${describe(el)}`);
        clipping(el, rect, true);
      }
      if (el.matches('[data-usage-percent]')) {
        if (!/^\\d+\\.\\d{2}%$/.test(el.textContent.trim()))
          failures.push(`split percentage: ${describe(el)}`);
        const tops = new Set(rects.map(rect => Math.round(rect.top)));
        if (tops.size !== 1) failures.push(`wrapped percentage: ${describe(el)}`);
      }
    }
  }
  for (const ring of document.querySelectorAll('[data-testid="usage-ring"]')) {
    if (!visible(ring)) continue;
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
    const style = getComputedStyle(el);
    if (parseFloat(style.fontSize) < 10) failures.push(`text below 10px: ${describe(el)}`);
    const card = el.closest('.usage-unit, [data-testid="usage-tile"], ' +
      '.ua-panel, .ua-kpi, .usage-pricing, .usage-board');
    const ellipsis = style.textOverflow === 'ellipsis' && el.title === el.textContent.trim();
    for (const rect of ellipsis ? [el.getBoundingClientRect()] : textRects(el)) {
      if (card && !inside(card.getBoundingClientRect(), rect))
        failures.push(`text outside card: ${describe(el)}`);
      clipping(el, rect, !ellipsis);
    }
  }
  if (document.documentElement.scrollWidth > innerWidth) failures.push('document overflow');
  return [...new Set(failures)];
}"""


def assert_usage_geometry(page: Page, expected: dict[str, int]) -> None:
    failures = page.evaluate(USAGE_GEOMETRY, expected)
    assert failures == [], (page.url, failures)
