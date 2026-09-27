"""Tests for :mod:`omnigent.artifact_paths`.

The bundle boundary is enforced on already-resolved paths, so these tests
pin the rules the server, runner and host all share: which components are
allowed, which names count as key material, and when a realpath counts as
strictly inside a bundle root.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnigent.artifact_paths import (
    artifact_path_allowed,
    artifact_segments_valid,
    resolved_within,
)


def test_segments_valid_accepts_ordinary_components() -> None:
    """A plain relative path passes."""
    assert artifact_segments_valid(("bundle", "assets", "style.css")) is True


@pytest.mark.parametrize(
    "segments",
    [
        (),
        ("bundle", ""),
        ("bundle", "."),
        ("bundle", ".."),
        ("..", "secret.txt"),
        ("dir\\file.txt",),
        ("dir\x00file.txt",),
    ],
)
def test_segments_valid_refuses_empty_relative_and_odd_components(
    segments: tuple[str, ...],
) -> None:
    """Empty paths, relative components, backslashes and NULs are invalid."""
    assert artifact_segments_valid(segments) is False


@pytest.mark.parametrize(
    "segments",
    [
        ("bundle", ".git", "config"),
        ("bundle", "assets", ".hidden"),
    ],
)
def test_path_allowed_refuses_dot_components(segments: tuple[str, ...]) -> None:
    """Dotfiles and dot-directories are never bundle sub-resources."""
    assert artifact_path_allowed(segments) is False


@pytest.mark.parametrize(
    "name",
    [
        "tls.pem",
        "TLS.PEM",
        "tls.key",
        "client.p12",
        "client.pfx",
        "prod.env",
        "id_rsa",
        "id_rsa.pub",
        "id_ecdsa",
        "id_ed25519",
        "ID_RSA",
    ],
)
def test_path_allowed_refuses_key_material_names_case_insensitively(name: str) -> None:
    """Every key-material convention is refused, whatever its case."""
    assert artifact_path_allowed(("reports", name)) is False


def test_path_allowed_accepts_ordinary_bundle_files() -> None:
    """A normal nested sub-resource still passes."""
    assert artifact_path_allowed(("reports", "assets", "index.html")) is True


def test_resolved_within_refuses_root_itself_and_outside() -> None:
    """The bundle root is not inside itself, and neither is a sibling path."""
    assert resolved_within(Path("/ws/bundle"), Path("/ws/bundle")) is False
    assert resolved_within(Path("/ws"), Path("/ws/bundle")) is False
    assert resolved_within(Path("/ws/other/notes.txt"), Path("/ws/bundle")) is False


def test_resolved_within_refuses_dot_component_in_relative_part() -> None:
    """The name rules are applied to the resolved path relative to the root."""
    assert resolved_within(Path("/ws/bundle/.git/config"), Path("/ws/bundle")) is False
    assert resolved_within(Path("/ws/bundle/keys/id_rsa"), Path("/ws/bundle")) is False


def test_resolved_within_accepts_ordinary_descendant() -> None:
    """A realpath strictly inside the root with an allowed relative path passes."""
    assert resolved_within(Path("/ws/bundle/sub/index.html"), Path("/ws/bundle")) is True
