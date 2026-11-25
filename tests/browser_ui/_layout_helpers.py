"""Chromium geometry for rendered DOM text and semantic controls in one scope."""

from pathlib import Path
from typing import TypedDict

from playwright.sync_api import FloatRect, Locator, expect

from tests.browser_ui.chat._geometry_helpers import intersection_size

_TOLERANCE = 2
_PROBE = """async (scope, tolerance) => {
    await document.fonts.ready;
    await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    const controls = 'button,input,textarea,select,a[href],summary,[role="button"],'
        + '[role="link"],[role="switch"],[role="checkbox"],[role="radio"],[role="combobox"],'
        + '[role="tab"],[role="slider"],[role^="menuitem"]';
    const elements = [scope, ...scope.querySelectorAll('*')];
    const indexes = new Map(elements.map((element, index) => [element, index]));
    const styles = new Map();
    const style = element => {
        if (!styles.has(element)) styles.set(element, getComputedStyle(element));
        return styles.get(element);
    };
    const visible = element =>
        element.checkVisibility({visibilityProperty: true, opacityProperty: true})
        && !element.closest('[inert]')
        && !/^rect\\(0px, 0px, 0px, 0px\\)$/.test(style(element).clip);
    const name = element => element.getAttribute('aria-label')
        || element.getAttribute('title') || element.textContent.trim().slice(0, 80)
        || element.getAttribute('name') || element.localName;
    const items = [];
    const problems = [];
    const contentOwner = element => element.closest(controls) || element.closest('label')?.control;
    const remember = (element, {left, top, right, bottom}, {
        description, owner, isControl = false, textRun = null,
    }) => {
        if (right > left && bottom > top) {
            items.push({
                description, anchor: indexes.get(element),
                rect: {x: left, y: top, width: right - left, height: bottom - top},
                owner: indexes.get(owner) ?? null, is_control: isControl,
                text_run: textRun,
                hit_area: isControl && element.getAttribute('data-layout-overlay') === 'hit-area',
                overlay: Boolean(
                    element.closest('[data-layout-overlay]:not([data-layout-overlay="hit-area"])')
                ),
            });
        }
    };
    const clips = new Map();
    const clipFor = element => {
        if (clips.has(element)) return clips.get(element);
        const css = style(element);
        let clip = null;
        if (css.clipPath === 'inset(50%)' || /^rect\\(0px, 0px, 0px, 0px\\)$/.test(css.clip)) {
            clip = {hidden: true};
        } else if (css.display !== 'inline' && css.display !== 'contents') {
            const clipX = /^(hidden|clip)$/.test(css.overflowX);
            const clipY = /^(hidden|clip)$/.test(css.overflowY);
            const scrollX = /^(auto|scroll)$/.test(css.overflowX)
                && element.scrollWidth > element.clientWidth;
            const scrollY = /^(auto|scroll)$/.test(css.overflowY)
                && element.scrollHeight > element.clientHeight;
            if (clipX || clipY || scrollX || scrollY) {
                const bounds = element.getBoundingClientRect();
                const left = bounds.left + element.clientLeft;
                const top = bounds.top + element.clientTop;
                clip = {
                    clipX, clipY, scrollX, scrollY, left, top,
                    right: left + element.clientWidth, bottom: top + element.clientHeight,
                    ellipsis: css.textOverflow.includes('ellipsis'),
                    lineClamp: +css.webkitLineClamp > 0,
                };
            }
        }
        clips.set(element, clip);
        return clip;
    };
    // A scroll container can reveal an entire line. A hidden/clip boundary cannot.
    const visibleRect = (rect, parent, description = null) => {
        let {left, right, top, bottom} = rect;
        for (; scope.contains(parent); parent = parent.parentElement) {
            const clip = clipFor(parent);
            if (!clip) continue;
            if (clip.hidden) return;
            const cutX = clip.clipX
                && (left < clip.left - tolerance || right > clip.right + tolerance);
            const cutY = clip.clipY
                && (top < clip.top - tolerance || bottom > clip.bottom + tolerance);
            if (description && (cutX || cutY) && !(cutX && !cutY && clip.ellipsis)
                && !clip.lineClamp) {
                problems.push({
                    anchor: description.anchor,
                    description:
                        `clipped: ${description.text} by ${parent.localName} "${name(parent)}"`,
                });
            }
            if (clip.clipX || clip.scrollX) {
                left = Math.max(left, clip.left);
                right = Math.min(right, clip.right);
            }
            if (clip.clipY || clip.scrollY) {
                top = Math.max(top, clip.top);
                bottom = Math.min(bottom, clip.bottom);
            }
            if (right <= left || bottom <= top) return;
        }
        return {left, right, top, bottom};
    };
    const range = document.createRange();
    const walker = document.createTreeWalker(scope, NodeFilter.SHOW_TEXT);
    let nextTextRun = 0;
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
        const element = node.parentElement;
        const value = node.textContent.trim();
        if (!value || !visible(element) || element.closest('script,style,select,textarea')) {
            continue;
        }
        range.setStart(node, node.textContent.indexOf(value));
        range.setEnd(node, node.textContent.indexOf(value) + value.length);
        const description = {anchor: indexes.get(element), text: `text "${value.slice(0, 100)}"`};
        const owner = contentOwner(element);
        const textRun = nextTextRun++;
        for (const rect of range.getClientRects()) {
            if (!rect.width || !rect.height) continue;
            const painted = visibleRect(rect, element, description);
            if (painted) {
                remember(element, painted, {description: description.text, owner, textRun});
            }
        }
    }
    for (const element of elements) {
        if (!visible(element)) continue;
        const isControl = element.matches(controls) && !element.matches('input[type=hidden]');
        const owner = element.matches('svg,img') ? element.closest(controls) : null;
        if (!isControl && !owner) continue;
        const description = isControl
            ? `${element.localName} "${name(element)}"`
            : `icon in "${name(owner)}"`;
        const painted = visibleRect(element.getBoundingClientRect(), element.parentElement);
        if (painted) remember(element, painted, {description, owner, isControl});
    }
    const overflow = scope.scrollWidth - scope.clientWidth;
    if (overflow > tolerance) {
        const right = scope.getBoundingClientRect().left + scope.clientLeft + scope.clientWidth;
        const protruding = elements.find(element =>
            visible(element) && !element.closest('[data-layout-overlay]')
            && element.getBoundingClientRect().right > right + tolerance);
        const description = protruding ? name(protruding) : 'scope content';
        problems.push({
            anchor: 0,
            description: `horizontal overflow: "${description}" ↔ scope edge (${overflow}px)`,
        });
    }
    return {items, problems};
}"""


class _Item(TypedDict):
    anchor: int
    description: str
    hit_area: bool
    is_control: bool
    owner: int | None
    overlay: bool
    rect: FloatRect
    text_run: int | None


class _Problem(TypedDict):
    anchor: int
    description: str


class _Reading(TypedDict):
    items: list[_Item]
    problems: list[_Problem]


def _contains(outer: FloatRect, inner: FloatRect) -> bool:
    return (
        outer["x"] <= inner["x"] + _TOLERANCE
        and outer["y"] <= inner["y"] + _TOLERANCE
        and outer["x"] + outer["width"] >= inner["x"] + inner["width"] - _TOLERANCE
        and outer["y"] + outer["height"] >= inner["y"] + inner["height"] - _TOLERANCE
    )


def _contains_related_content(outer: _Item, inner: _Item) -> bool:
    # Chromium can return both the original and ellipsized rect for one text
    # node. A declared hit area likewise surrounds smaller, independent actions.
    same_text = outer["text_run"] is not None and outer["text_run"] == inner["text_run"]
    own_content = outer["is_control"] and inner["owner"] == outer["anchor"]
    return (same_text or own_content or outer["hit_area"]) and _contains(
        outer["rect"], inner["rect"]
    )


def assert_layout(scope: Locator, surface: str, output: Path) -> None:
    """Assert the contract and capture evidence at each distinct hit location."""
    expect(scope).to_be_visible()
    reading: _Reading = scope.evaluate(_PROBE, _TOLERANCE)
    problems = reading["problems"]
    items = sorted(reading["items"], key=lambda item: item["rect"]["y"])
    assert items, f"{surface}: no visible text or controls"
    for index, first in enumerate(items):
        left = first["rect"]
        for next_index in range(index + 1, len(items)):
            second = items[next_index]
            right = second["rect"]
            if right["y"] >= left["y"] + left["height"] - _TOLERANCE:
                break
            if (
                first["overlay"]
                or second["overlay"]
                or _contains_related_content(first, second)
                or _contains_related_content(second, first)
            ):
                continue
            overlap_x, overlap_y = intersection_size(left, right)
            if min(overlap_x, overlap_y) > _TOLERANCE:
                problems.append(
                    {
                        "anchor": first["anchor"],
                        "description": (
                            f"overlap: {first['description']} ↔ {second['description']} "
                            f"({overlap_x:.1f} × {overlap_y:.1f}px)"
                        ),
                    }
                )

    if not problems:
        return
    width = scope.page.evaluate("window.innerWidth")
    screenshots: dict[int, Path] = {}
    messages: list[str] = []
    for hit in problems:
        anchor = hit["anchor"]
        if anchor not in screenshots:
            scope.evaluate(
                """(scope, index) => {
                    const element = index === 0 ? scope : scope.querySelectorAll('*')[index - 1];
                    element?.scrollIntoView({
                        block: 'center', inline: 'nearest', behavior: 'instant',
                    });
                }""",
                anchor,
            )
            path = output / f"{surface}-{width}-{anchor}.png"
            scope.page.screenshot(path=path, animations="disabled")
            screenshots[anchor] = path
        messages.append(f"{surface} {width}px {hit['description']} — {screenshots[anchor]}")
    raise AssertionError("\n".join(dict.fromkeys(messages)))
