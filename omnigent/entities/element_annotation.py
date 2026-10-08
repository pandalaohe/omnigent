"""Validation and clamping for element-annotation anchors.

An element annotation is an ordinary comment whose ``anchor_content`` is
:data:`ELEMENT_ANCHOR_PREFIX` followed by a JSON payload built in the
annotating frame. Page scripts share that frame's realm, so the payload is
untrusted and is re-validated here before it is stored or shown to the agent.
The module is pure stdlib so the comment store and the comments route share it
without importing each other; ``web/src/shell/annotationAnchor.ts`` mirrors
these rules.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from typing import Any, Final
from urllib.parse import urlsplit, urlunsplit

#: Prefix marking ``anchor_content`` as an element-annotation anchor.
ELEMENT_ANCHOR_PREFIX: Final = "__element__"

_MAX_ANCHOR_BYTES = 32 * 1024
_MAX_URL = 2000
_MAX_CONSOLE_ENTRIES = 50
_MAX_NETWORK_ENTRIES = 20
_GEOMETRY_LIMIT = 1_000_000

_TARGET_STRING_CAPS: Final[dict[str, int]] = {
    "label": 200,
    "css": 700,
    "xpath": 900,
    "fingerprint": 120,
    "neighborText": 80,
    "tag": 32,
    "id": 191,
    "role": 64,
    "ariaLabel": 200,
    "text": 200,
}
_QUOTE_STRING_CAPS: Final[dict[str, int]] = {"exact": 200, "prefix": 32, "suffix": 32}
_GEOMETRY_KEYS: Final = ("x", "y", "w", "h")
_FILE_ID_RE: Final = re.compile(r"[A-Za-z0-9_-]{1,64}")


def is_element_anchor_prefix(anchor_content: str | None) -> bool:
    """Whether *anchor_content* carries the element-anchor marker.

    The check never parses or validates the payload; a producer that wants
    to know whether it *should* parse uses this, while a consumer that needs
    the payload calls :func:`parse_element_anchor`.

    :param anchor_content: The stored comment anchor, or ``None``.
    :returns: ``True`` when the value starts with the prefix.
    """
    return isinstance(anchor_content, str) and anchor_content.startswith(ELEMENT_ANCHOR_PREFIX)


def parse_element_anchor(anchor_content: str | None) -> dict[str, Any] | None:
    """Parse, validate, and clamp an element anchor.

    Returns ``None`` when *anchor_content* is not an element anchor, is not
    JSON, is not an object, fails the required-field check (``v`` must be the
    number ``1``; ``kind`` must be ``"element"`` or ``"region"``; ``rect``
    must carry all four finite sides), carries a non-finite number anywhere,
    or exceeds 32 KiB after clamping. A valid payload comes back as a new
    dict holding only the known fields: strings coerced to one line and
    capped (an over-cap string is cut so the result, ellipsis included, fits
    the cap, and lone surrogates become U+FFFD), geometry clamped to ±1e6,
    timestamps as integer milliseconds, console and network cut to their
    newest entries, and a screenshot descriptor or ``None``. An invalid
    screenshot does not invalidate the anchor — it is dropped, and the
    annotation still renders.

    :param anchor_content: The stored comment anchor, or ``None``.
    :returns: The validated, clamped anchor, or ``None``.
    """
    if not isinstance(anchor_content, str) or not anchor_content.startswith(ELEMENT_ANCHOR_PREFIX):
        return None
    try:
        raw = json.loads(anchor_content[len(ELEMENT_ANCHOR_PREFIX) :])
    except (ValueError, RecursionError):
        return None
    if not isinstance(raw, dict) or raw.get("kind") not in ("element", "region"):
        return None
    if raw.get("v") != 1 or isinstance(raw.get("v"), bool):
        return None
    # JSON.stringify turns NaN/Infinity into null, so a non-finite number can
    # only come from a hand-crafted payload; reject the anchor rather than
    # persist a value no strict JSON consumer can read back.
    try:
        if _has_non_finite_number(raw):
            return None
    except RecursionError:
        return None

    kind = raw["kind"]
    result: dict[str, Any] = {"v": 1, "kind": kind}
    page = _parse_page(raw.get("page"))
    if page:
        result["page"] = page
    target = _parse_target(raw.get("target"))
    if target:
        result["target"] = target
    rect = _parse_rect(raw.get("rect"))
    if rect is None:
        return None
    result["rect"] = rect
    if kind == "region":
        region = _parse_geometry(raw.get("region"))
        if region:
            result["region"] = region
        selected_text = _string(raw.get("selectedText"), 500) if "selectedText" in raw else None
        if selected_text is not None:
            result["selectedText"] = selected_text
    console = _parse_console(raw.get("console"))
    if console:
        result["console"] = console
    network = _parse_network(raw.get("network"))
    if network:
        result["network"] = network
    result["screenshot"] = _parse_screenshot(raw.get("screenshot"))

    encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > _MAX_ANCHOR_BYTES:
        return None
    return result


def element_anchor_evidence(anchor: Mapping[str, Any]) -> str:
    """Render the page-derived anchor fields as compact untrusted JSON.

    Only the fields a resolver or a reader needs are included; the comment
    body and the screenshot reference are transport metadata, not page
    evidence. Every ``</`` is escaped so page text cannot close the
    surrounding ``<untrusted_page_evidence>`` wrapper.

    :param anchor: A parsed element anchor.
    :returns: One compact JSON line.
    """
    evidence: dict[str, Any] = {"kind": anchor.get("kind")}
    page = anchor.get("page")
    if isinstance(page, Mapping) and "url" in page:
        evidence["page"] = {"url": page["url"]}
    target = anchor.get("target")
    if isinstance(target, Mapping):
        target_evidence = {
            key: target[key]
            for key in ("label", "css", "xpath", "quote", "tag", "role", "ariaLabel", "text")
            if key in target
        }
        if target_evidence:
            evidence["target"] = target_evidence
    for key in ("rect", "region", "selectedText", "console", "network"):
        if key in anchor:
            evidence[key] = anchor[key]
    return json.dumps(evidence, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")


def element_anchor_screenshot(anchor: Mapping[str, Any]) -> dict[str, Any] | None:
    """The screenshot descriptor carried by a parsed anchor.

    :param anchor: A parsed element anchor.
    :returns: ``{"file_id", "filename", "width", "height"}``, or ``None``
        when the anchor has no screenshot.
    """
    screenshot = anchor.get("screenshot")
    return dict(screenshot) if isinstance(screenshot, Mapping) else None


def _has_non_finite_number(value: Any) -> bool:
    """Whether *value* holds a non-finite float anywhere in the structure.

    :param value: A decoded JSON value.
    :returns: ``True`` for a non-finite float (``NaN`` / ``Infinity``).
    """
    if isinstance(value, float):
        return not math.isfinite(value)
    if isinstance(value, dict):
        return any(_has_non_finite_number(item) for item in value.values())
    if isinstance(value, list):
        return any(_has_non_finite_number(item) for item in value)
    return False


def _one_line(value: Any) -> str | None:
    """Coerce a string to a single line, splitting at every Unicode boundary.

    :param value: The candidate string.
    :returns: The one-line string, or ``None`` when *value* is not a string.
    """
    if not isinstance(value, str):
        return None
    return " ".join(value.splitlines())


def _replace_lone_surrogates(value: str) -> str:
    """Replace every lone surrogate code point with U+FFFD.

    ``json.loads`` keeps an unpaired ``\\uD800``-style escape as a lone
    surrogate code point, which cannot be UTF-8 encoded; a valid pair is
    already one code point here.

    :param value: The string to sanitize.
    :returns: *value* with every surrogate code point replaced.
    """
    if not any(0xD800 <= ord(char) <= 0xDFFF for char in value):
        return value
    return "".join("\ufffd" if 0xD800 <= ord(char) <= 0xDFFF else char for char in value)


def _cap(value: str, cap: int) -> str:
    """Sanitize and cut *value* so the result, ellipsis included, fits *cap*.

    :param value: The string to cap.
    :param cap: Maximum result length.
    :returns: The sanitized string, or its capped form ending in ``…``.
    """
    value = _replace_lone_surrogates(value)
    if len(value) <= cap:
        return value
    return value[: cap - 1] + "…"


def _string(value: Any, cap: int) -> str | None:
    """Validate and cap a one-line string field.

    :param value: The candidate value.
    :param cap: Maximum result length.
    :returns: The capped one-line string, or ``None`` when invalid.
    """
    text = _one_line(value)
    return None if text is None else _cap(text, cap)


def _number(value: Any) -> int | float | None:
    """Validate a finite JSON number.

    :param value: The candidate value.
    :returns: The number, or ``None`` when not finite (an int too large for
        a float raises ``OverflowError`` inside ``math.isfinite``).
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return value if math.isfinite(value) else None
    except OverflowError:
        return None


def _timestamp(value: Any) -> int | None:
    """Validate a timestamp as integer milliseconds.

    :param value: The candidate value.
    :returns: The rounded millisecond value, or ``None`` when not finite.
    """
    number = _number(value)
    return None if number is None else round(number)


def _bounded_int(value: Any, low: int, high: int) -> int | None:
    """Validate an integer inside an inclusive range.

    :param value: The candidate value.
    :param low: Inclusive lower bound.
    :param high: Inclusive upper bound.
    :returns: The integer, or ``None`` when out of range or not integral.
    """
    number = _number(value)
    if number is None or int(number) != number or not low <= number <= high:
        return None
    return int(number)


def _sanitized_url(value: Any) -> str | None:
    """Reduce a URL to http(s) scheme, host, and path.

    Query, fragment, and credentials are dropped; any other scheme (or a
    missing host) becomes the empty string.

    :param value: The candidate value.
    :returns: The sanitized URL, or ``None`` when *value* is not a string.
    """
    if not isinstance(value, str):
        return None
    try:
        parts = urlsplit(value)
    except ValueError:
        return ""
    if parts.scheme not in ("http", "https"):
        return ""
    netloc = parts.netloc.rpartition("@")[2]
    if not netloc:
        return ""
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


def _parse_rect(value: Any) -> dict[str, int | float] | None:
    """Validate the required ``rect`` mapping, clamping each side to ±1e6.

    All four sides must be present and finite, mirroring the parent codec:
    a partial or malformed rect invalidates the whole anchor.

    :param value: The candidate mapping.
    :returns: The clamped rect, or ``None`` when invalid.
    """
    if not isinstance(value, dict):
        return None
    rect: dict[str, int | float] = {}
    for key in _GEOMETRY_KEYS:
        number = _number(value.get(key))
        if number is None:
            return None
        rect[key] = max(-_GEOMETRY_LIMIT, min(_GEOMETRY_LIMIT, number))
    return rect


def _parse_geometry(value: Any) -> dict[str, int | float] | None:
    """Validate an optional geometry mapping, clamping each value to ±1e6.

    :param value: The candidate mapping.
    :returns: The clamped mapping, or ``None`` when no keys are valid.
    """
    if not isinstance(value, dict):
        return None
    geometry: dict[str, int | float] = {}
    for key in _GEOMETRY_KEYS:
        if key not in value:
            continue
        number = _number(value[key])
        if number is not None:
            geometry[key] = max(-_GEOMETRY_LIMIT, min(_GEOMETRY_LIMIT, number))
    return geometry or None


def _parse_page(value: Any) -> dict[str, Any] | None:
    """Validate the page context mapping.

    :param value: The candidate mapping.
    :returns: The validated mapping, or ``None`` when empty or invalid.
    """
    if not isinstance(value, dict):
        return None
    page: dict[str, Any] = {}
    if "url" in value:
        url = _sanitized_url(value["url"])
        if url is not None:
            page["url"] = _cap(url, _MAX_URL)
    title = _string(value.get("title"), 200) if "title" in value else None
    if title is not None:
        page["title"] = title
    for key in ("vw", "vh", "sx", "sy"):
        if key in value:
            number = _number(value[key])
            if number is not None:
                page[key] = max(-_GEOMETRY_LIMIT, min(_GEOMETRY_LIMIT, number))
    if "dpr" in value:
        number = _number(value["dpr"])
        if number is not None:
            page["dpr"] = number
    return page or None


def _parse_target(value: Any) -> dict[str, Any] | None:
    """Validate the target descriptor mapping.

    :param value: The candidate mapping.
    :returns: The validated mapping, or ``None`` when empty or invalid.
    """
    if not isinstance(value, dict):
        return None
    target: dict[str, Any] = {}
    for key, cap in _TARGET_STRING_CAPS.items():
        if key in value:
            text = _string(value[key], cap)
            if text is not None:
                target[key] = text
    quote = value.get("quote")
    if isinstance(quote, dict):
        validated_quote: dict[str, str] = {}
        for key, cap in _QUOTE_STRING_CAPS.items():
            text = _string(quote.get(key), cap)
            if text is not None:
                validated_quote[key] = text
        if validated_quote:
            target["quote"] = validated_quote
    return target or None


def _parse_console(value: Any) -> list[dict[str, Any]] | None:
    """Validate console diagnostics, keeping the newest entries.

    :param value: The candidate list.
    :returns: At most 50 validated entries, or ``None`` when none are valid.
    """
    if not isinstance(value, list):
        return None
    entries: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict) or item.get("level") not in ("error", "warn"):
            continue
        message = _string(item.get("message"), 500)
        timestamp = _timestamp(item.get("ts"))
        if message is None or timestamp is None:
            continue
        entries.append({"level": item["level"], "message": message, "ts": timestamp})
    return entries[-_MAX_CONSOLE_ENTRIES:] or None


def _parse_network(value: Any) -> list[dict[str, Any]] | None:
    """Validate failed-request diagnostics, keeping the newest entries.

    :param value: The candidate list.
    :returns: At most 20 validated entries, or ``None`` when none are valid.
    """
    if not isinstance(value, list):
        return None
    entries: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        method = _string(item.get("method"), 20)
        url = _sanitized_url(item.get("url"))
        status = _number(item.get("status"))
        timestamp = _timestamp(item.get("ts"))
        if method is None or url is None or status is None or timestamp is None:
            continue
        entries.append(
            {
                "method": method,
                "url": _cap(url, _MAX_URL),
                "status": max(0, min(599, round(status))),
                "ts": timestamp,
            }
        )
    return entries[-_MAX_NETWORK_ENTRIES:] or None


def _parse_screenshot(value: Any) -> dict[str, Any] | None:
    """Validate a screenshot descriptor.

    :param value: The candidate mapping.
    :returns: The validated descriptor, or ``None`` when invalid.
    """
    if not isinstance(value, dict):
        return None
    file_id = value.get("file_id")
    if not isinstance(file_id, str) or _FILE_ID_RE.fullmatch(file_id) is None:
        return None
    filename = _string(value.get("filename"), 128)
    width = _bounded_int(value.get("width"), 1, 4096)
    height = _bounded_int(value.get("height"), 1, 4096)
    if filename is None or width is None or height is None:
        return None
    return {"file_id": file_id, "filename": filename, "width": width, "height": height}
