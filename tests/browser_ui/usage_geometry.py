"""Visible usage figures must fit their cards, including narrow previews."""

from playwright.sync_api import Page

USAGE_GEOMETRY = """() => {
  const failures = [];
  const visible = el => el.checkVisibility() &&
    el.getClientRects().length && el.getBoundingClientRect().width;
  const inside = (outer, inner) => inner.left >= outer.left - 1 &&
    inner.right <= outer.right + 1 && inner.top >= outer.top - 1 &&
    inner.bottom <= outer.bottom + 1;
  const describe = el => `${el.tagName}.${el.className?.baseVal ?? el.className}: ` +
    el.textContent?.trim().slice(0, 80);
  const figures = '[data-testid="usage-ring"], .usage-balance-ring, [data-usage-amount]';
  for (const el of document.querySelectorAll(figures)) {
    if (!visible(el)) continue;
    const card = el.closest('.usage-unit, [data-usage-window], ' +
      '[data-testid="usage-tile"], [data-testid="usage-balances"]');
    if (card && !inside(card.getBoundingClientRect(), el.getBoundingClientRect()))
      failures.push(`figure outside tile: ${describe(el)}`);
    const tile = el.closest('[data-testid="usage-tile"]');
    if (tile && !inside(tile.getBoundingClientRect(), el.getBoundingClientRect()))
      failures.push(`figure outside account: ${describe(el)}`);
    const range = document.createRange(); range.selectNodeContents(el);
    for (const rect of range.getClientRects())
      if (!inside(el.getBoundingClientRect(), rect))
        failures.push(`text outside figure: ${describe(el)}`);
  }
  for (const ring of document.querySelectorAll('[data-testid="usage-ring"]')) {
    if (!visible(ring)) continue;
    for (const circle of ring.querySelectorAll('circle'))
      if (!inside(ring.getBoundingClientRect(), circle.getBoundingClientRect()))
        failures.push(`arc outside ring: ${describe(ring)}`);
    const text = [...ring.querySelectorAll('span')].find(el =>
      !el.children.length && /^\\d+\\.\\d{2}%$/.test(el.textContent.trim()));
    if (ring.textContent.includes('%') && !text)
      failures.push(`split percentage: ${describe(ring)}`);
    if (text) {
      const range = document.createRange(); range.selectNodeContents(text);
      const tops = new Set([...range.getClientRects()].map(rect => Math.round(rect.top)));
      if (tops.size !== 1) failures.push(`wrapped percentage: ${describe(text)}`);
    }
  }
  const texts = '.usage-board :is(p, b, strong, label, button, a, small, span)';
  for (const el of document.querySelectorAll(texts)) {
    if (!visible(el) || el.children.length || el.closest('[data-testid="usage-tip-card"]'))
      continue;
    const style = getComputedStyle(el);
    if (style.overflowX === 'hidden') continue;
    const range = document.createRange(); range.selectNodeContents(el);
    const card = el.closest('.usage-unit, [data-testid="usage-tile"], ' +
      '.ua-panel, .ua-kpi, .usage-pricing');
    if (card) for (const rect of range.getClientRects())
      if (!inside(card.getBoundingClientRect(), rect))
        failures.push(`text outside card: ${describe(el)}`);
  }
  if (document.documentElement.scrollWidth > innerWidth) failures.push('document overflow');
  return [...new Set(failures)];
}"""


def assert_usage_geometry(page: Page) -> None:
    failures = page.evaluate(USAGE_GEOMETRY)
    assert failures == [], (page.url, failures)
