"""Every reference consumer uses the same usable-catalog boundary."""

import pytest

from omnigent.models.model_reference import provider_groups
from omnigent.server.model_resolution import ResolutionContext, notes_from_labels, usable_catalog


@pytest.mark.parametrize(
    "record",
    [
        None,
        {},
        {"models": []},
        {"models": [{"id": "p/x"}], "error": "probe failed"},
        {"models": [{"id": ""}, {"model": None}, "invalid"]},
        {"models": None},
    ],
)
def test_unusable_catalog_is_unknown(record):
    assert usable_catalog(record) is None


def test_usable_catalog_includes_both_advertised_identifiers():
    assert usable_catalog(
        {"models": [{"id": "p/x", "model": "x"}, {"model": "p/y"}], "error": None}
    ) == frozenset({"p/x", "x", "p/y"})


def test_adapter_preserves_relay_route_and_reverses_on_an_uncut_host():
    groups = provider_groups([("old", "krill")])
    context = ResolutionContext("host-a", {"codex-native": {"models": [{"id": "x"}]}}, groups)
    result = context.resolve("codex-native", "old/x", "explicit")
    assert (result.model, result.row_id, result.outcome) == ("krill/x", "x", "pinned")
    context.records = {"codex-native": {"models": [{"id": "old/x"}, {"id": "x"}]}}
    reverse = context.resolve("codex-native", "krill/x", "resume")
    assert (reverse.model, reverse.row_id, reverse.outcome) == ("old/x", "old/x", "renamed")


def test_missing_is_a_note_not_a_changed_launch_and_notes_fit_label_columns():
    context = ResolutionContext("host-a", {"pi-native": {"models": [{"id": "other/y"}]}})
    for index in range(4):
        result = context.resolve("pi-native", f"old/x-{index}", f"member:{index}")
        assert result.model == result.saved and result.outcome == "missing"
    labels = context.labels()
    assert all(len(value) <= 256 for value in labels.values())
    assert notes_from_labels(labels) == [note.payload() for note in context.notes]
    assert all("trying it as unknown" in warning for warning in context.warnings())
