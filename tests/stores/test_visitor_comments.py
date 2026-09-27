"""Tests for the visitor-comment helpers in ``comment_store.visitor_comments``.

The helpers are importable from the runner side (no server route stack), so
they live under the comment store; these tests exercise the author-marker
family's cleaning and matching rules directly.
"""

from __future__ import annotations

import pytest

from omnigent.stores.comment_store import visitor_comments

# ── visitor author marker ───────────────────────────────────────────


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Alice", "visitor:Alice"),
        ("  Alice  ", "visitor:Alice"),
        ("Ali\nce", "visitor:Ali" + "ce"),
        ("bob:builder", "visitor:bobbuilder"),
        ("", "visitor:"),
        (None, "visitor:"),
        ("x" * 60, "visitor:" + "x" * 40),
    ],
)
def test_visitor_author_cleans_the_submitted_name(name: str | None, expected: str) -> None:
    """Control characters and ``:`` cannot survive into the author marker."""
    assert visitor_comments.visitor_author(name) == expected


@pytest.mark.parametrize(
    ("created_by", "expected"),
    [
        ("visitor:Alice", True),
        ("visitor:", True),
        ("alice@example.com", False),
        (None, False),
    ],
)
def test_is_visitor_author_matches_only_the_marker_family(
    created_by: str | None, expected: bool
) -> None:
    """Account ids never carry the marker; the marker always does."""
    assert visitor_comments.is_visitor_author(created_by) is expected
