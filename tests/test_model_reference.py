"""Saved full model ids resolve without changing their persisted shape."""

import pytest

from omnigent.models.model_reference import (
    canonical_model,
    canonical_pool_values,
    equivalent_ids,
    provider_groups,
    resolve_saved_model,
    split_model_id,
)


@pytest.mark.parametrize(
    "model,expected",
    [("model-x", (None, "model-x")), ("p/x/y", ("p", "x/y")), ("", (None, ""))],
)
def test_split_preserves_the_model_suffix(model, expected):
    assert split_model_id(model) == expected


def test_transitive_bidirectional_groups_and_canonical_identity():
    groups = provider_groups([("old", "middle"), ("new", "middle"), ("solo", "solo")])
    assert equivalent_ids("new/x/y", groups) == ("new/x/y", "middle/x/y", "old/x/y")
    assert equivalent_ids("old/x/y", groups) == ("old/x/y", "middle/x/y", "new/x/y")
    assert canonical_model("old/x/y", groups) == "middle/x/y"
    assert canonical_model("new/x/y", groups) == "middle/x/y"
    assert canonical_model("other/x", groups) == "other/x"
    assert equivalent_ids("plain", groups) == ("plain",)


def test_pool_collisions_prefer_the_saved_key_then_the_first_raw_key():
    groups = provider_groups([("old", "new")])
    values = {"pi:old/x": 1, "pi:new/x": 2, "other": 3}
    assert canonical_pool_values(values, groups) == {"pi:new/x": 2, "other": 3}
    assert canonical_pool_values(values, groups, preferred="pi:old/x") == {
        "pi:new/x": 1,
        "other": 3,
    }
    assert values == {"pi:old/x": 1, "pi:new/x": 2, "other": 3}


@pytest.mark.parametrize(
    "catalog,outcome,model,row,candidates",
    [
        (None, "unverified", "old/x", None, ()),
        (frozenset(), "unverified", "old/x", None, ()),
        ({"old/x", "new/x"}, "exact", "old/x", "old/x", ()),
        ({"new/x", "other/x"}, "renamed", "new/x", "new/x", ()),
        ({"other/x", "other/y"}, "substituted", "other/x", "other/x", ()),
        ({"b/x", "a/x"}, "missing", "old/x", None, ("a/x", "b/x")),
        ({"other/y"}, "missing", "old/x", None, ()),
    ],
)
def test_resolution_precedence(catalog, outcome, model, row, candidates):
    result = resolve_saved_model("old/x", catalog, provider_groups([("old", "new")]))
    assert (result.saved, result.outcome, result.model, result.row_id, result.candidates) == (
        "old/x",
        outcome,
        model,
        row,
        candidates,
    )


def test_relay_alias_preserves_route_and_prefers_a_listed_equivalent():
    groups = provider_groups([("old", "relay")])

    def accepts(model):
        return "x" if model == "relay/x" else None

    forward = resolve_saved_model("old/x", {"x"}, groups, accepts=accepts)
    assert (forward.outcome, forward.model, forward.row_id) == ("pinned", "relay/x", "x")
    reverse = resolve_saved_model("relay/x", {"old/x", "x"}, groups, accepts=accepts)
    assert (reverse.outcome, reverse.model, reverse.row_id) == ("renamed", "old/x", "old/x")


def test_pin_callback_cannot_validate_a_row_absent_from_the_catalog():
    result = resolve_saved_model("relay/x", {"other/y"}, {}, accepts=lambda _: "x")
    assert result.outcome == "missing" and result.model == "relay/x"


@pytest.mark.parametrize(
    "saved,catalog,candidates",
    [("relay/x", {"x"}, ("x",)), ("x", {"relay/x"}, ("relay/x",))],
)
def test_substitution_can_be_disabled_for_launches(saved, catalog, candidates):
    result = resolve_saved_model(saved, catalog, {}, allow_substitution=False)
    assert (result.outcome, result.model, result.row_id, result.candidates) == (
        "missing",
        saved,
        None,
        candidates,
    )


def test_full_suffix_equality_does_not_merge_different_models():
    result = resolve_saved_model("old/family/x", {"p/x", "p/family/x"}, {})
    assert result.model == "p/family/x" and result.outcome == "substituted"


@pytest.mark.parametrize("pairs", [[("", "p")], [("a/b", "p")], [("p", " ")]])
def test_aliases_reject_names_that_cannot_identify_a_provider(pairs):
    with pytest.raises(ValueError):
        provider_groups(pairs)
