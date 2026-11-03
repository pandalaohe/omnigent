"""The version line names a stamped distribution build; source installs are unchanged."""

from __future__ import annotations

from pathlib import Path

import pytest

import omnigent.distribution as distribution
import omnigent.update_check as update_check
from omnigent.cli import _format_version
from omnigent.version import VERSION


@pytest.fixture(autouse=True)
def _no_build_info(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(update_check, "_read_build_info", lambda: None)


def test_version_line_names_the_distribution_when_stamped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(distribution, "DISTRIBUTION_VERSION", "0.21.0-wukong.12")

    assert _format_version() == f"omnigent {VERSION} (omnigent-wukong 0.21.0-wukong.12)"


def test_version_line_is_unchanged_without_a_stamp() -> None:
    assert distribution.DISTRIBUTION_VERSION is None
    assert _format_version() == f"omnigent {VERSION}"


def test_stamp_line_is_unique_for_the_snapshot_builder() -> None:
    """The builder rewrites exactly one ``DISTRIBUTION_VERSION`` assignment."""
    source = Path(distribution.__file__).read_text(encoding="utf-8")

    assert source.count("DISTRIBUTION_VERSION: str | None = None\n") == 1
