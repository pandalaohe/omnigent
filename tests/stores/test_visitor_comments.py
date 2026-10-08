"""Tests for the visitor-comment helpers in ``comment_store.visitor_comments``.

The helpers are importable from the runner side (no server route stack), so
they live under the comment store; these tests exercise the author-marker
family's cleaning and matching rules directly.
"""

from __future__ import annotations

import pytest

from omnigent.entities import Comment
from omnigent.entities.element_annotation import ELEMENT_ANCHOR_PREFIX
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


# ── comment_rows ────────────────────────────────────────────────────


def _comment(comment_id: str, created_by: str | None, anchor_content: str | None) -> Comment:
    """Build a minimal comment for ``comment_rows`` tests.

    :param comment_id: Unique comment id.
    :param created_by: Author marker, or ``None`` for an owner row.
    :param anchor_content: Stored anchor content.
    :returns: A :class:`Comment`.
    """
    return Comment(
        id=comment_id,
        conversation_id="conv_test",
        path="reports/q3.html",
        start_index=0,
        end_index=0,
        body="note",
        status="draft",
        created_at=0,
        updated_at=0,
        anchor_content=anchor_content,
        created_by=created_by,
    )


def test_comment_rows_adds_annotation_to_owner_element_rows_only() -> None:
    """Only an owner row with a parsable anchor gains the ``annotation`` key."""
    anchor_content = (
        ELEMENT_ANCHOR_PREFIX
        + '{"v":1,"kind":"element","rect":{"x":0,"y":0,"w":1,"h":1},"target":{"label":"div.a"}}'
    )
    rows = {
        row["id"]: row
        for row in visitor_comments.comment_rows(
            [
                _comment("owner-element", None, anchor_content),
                _comment("visitor-element", "visitor:Alice", anchor_content),
                _comment("owner-text", None, "plain selected text"),
                _comment("owner-bad", None, ELEMENT_ANCHOR_PREFIX + '{"v":2}'),
            ],
            include_visitor_drafts=True,
        )
    }

    assert rows["owner-element"]["annotation"]["target"]["label"] == "div.a"
    assert "annotation" not in rows["visitor-element"]
    assert "annotation" not in rows["owner-text"]
    assert "annotation" not in rows["owner-bad"]
    assert rows["visitor-element"]["source"] == visitor_comments.VISITOR_SOURCE
