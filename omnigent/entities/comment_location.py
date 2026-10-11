"""Interpret text and PDF review anchors without mistaking geometry for source offsets."""

from __future__ import annotations

import json
import math
from typing import Any, TypeGuard

from omnigent.entities.comment import Comment

TEXT_ANCHOR_PREFIX = "__text__"
MAX_TEXT_ANCHOR_BYTES = 32 * 1024
_PDF_PREFIX = "__pdf__"


def _integer(value: object, maximum: int = 2**53 - 1) -> TypeGuard[int]:
    return type(value) is int and 0 <= value <= maximum


def _bounded_text(value: object, maximum: int) -> TypeGuard[str]:
    if not isinstance(value, str):
        return False
    try:
        return len(value.encode("utf-16-le")) // 2 <= maximum
    except UnicodeError:
        return False


def parse_text_anchor(content: str | None) -> dict[str, Any] | None:
    """Validate a rendered quote, retaining only the bounded locator fields.

    :param content: A stored anchor, including the ``__text__`` prefix.
    :returns: The v1 locator, or ``None`` for a malformed or different anchor.
    """
    if not content or not content.startswith(TEXT_ANCHOR_PREFIX):
        return None
    try:
        if len(content.encode("utf-8")) > MAX_TEXT_ANCHOR_BYTES:
            return None
        value = json.loads(content[len(TEXT_ANCHOR_PREFIX) :])
    except (ValueError, UnicodeError, RecursionError):
        return None
    if not isinstance(value, dict) or type(value.get("v")) is not int or value["v"] != 1:
        return None
    if value.get("kind") not in ("html", "markdown") or not _integer(value.get("occ")):
        return None
    for field, cap in (("text", 16000), ("prefix", 32), ("suffix", 32)):
        item = value.get(field)
        if not _bounded_text(item, cap):
            return None
    if not value["text"].strip():
        return None
    result = {key: value[key] for key in ("v", "kind", "text", "occ", "prefix", "suffix")}
    if "selector" in value:
        if not _bounded_text(value["selector"], 700):
            return None
        result["selector"] = value["selector"]
    if "source" in value:
        source = value["source"]
        if not isinstance(source, dict):
            return None
        start, end = source.get("start_index"), source.get("end_index")
        if not _integer(start) or not _integer(end) or end <= start:
            return None
        result["source"] = {"start_index": start, "end_index": end}
    return result


def _pdf_anchor(content: str) -> dict[str, Any] | None:
    try:
        if len(content.encode("utf-8")) > MAX_TEXT_ANCHOR_BYTES:
            return None
        value = json.loads(content[len(_PDF_PREFIX) :])
    except (ValueError, UnicodeError, RecursionError):
        return None
    if not isinstance(value, dict) or not _integer(value.get("page")) or value["page"] < 1:
        return None
    text, rects = value.get("text"), value.get("rects")
    if not _bounded_text(text, 16000):
        return None
    if not isinstance(rects, list) or not rects or len(rects) > 1000:
        return None
    clean_rects = []
    for rect in rects:
        if not isinstance(rect, dict):
            return None
        for key in ("x", "y", "w", "h"):
            number = rect.get(key)
            if not isinstance(number, (float, int)) or isinstance(number, bool):
                return None
            if abs(number) > 1e6 or not math.isfinite(number):
                return None
        if rect["w"] <= 0 or rect["h"] <= 0:
            return None
        clean_rects.append({key: rect[key] for key in ("x", "y", "w", "h")})
    return {"kind": "pdf", "page": value["page"], "rects": clean_rects, "text": text}


def comment_location(comment: Comment) -> dict[str, Any]:
    """Describe a comment's coordinate space for send and list consumers.

    :param comment: A stored review comment.
    :returns: A source range, rendered locator, PDF geometry, whole-page or unknown location.
    """
    content = comment.anchor_content or ""
    if content.startswith(TEXT_ANCHOR_PREFIX):
        return parse_text_anchor(content) or {"kind": "unknown", "text": ""}
    if content.startswith(_PDF_PREFIX):
        return _pdf_anchor(content) or {"kind": "unknown", "text": ""}
    if comment.start_index == comment.end_index == 0:
        return {"kind": "unknown" if content else "whole-page"}
    return {"kind": "source", "start_index": comment.start_index, "end_index": comment.end_index}
