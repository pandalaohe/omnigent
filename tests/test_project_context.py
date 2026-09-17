"""Tests for project-context manifest parsing and digest.

Lives at ``tests/`` top level rather than ``tests/spec/``: that directory
covers the agent-spec parser only (``test_parser.py``,
``test_validator.py``, ...), not project manifests.
"""

from __future__ import annotations

import json

import pytest

from omnigent.project_context import (
    THIS_REPOSITORY_KEY,
    CrossRepoEntry,
    ManifestError,
    ProjectManifest,
    manifest_digest,
    parse_manifest,
    required_paths,
    validate_manifest_path,
)


def _blob(payload: object) -> bytes:
    """Encode a manifest payload as JSON bytes."""
    return json.dumps(payload).encode("utf-8")


def test_digest_known_value() -> None:
    """The digest is ``sha256:`` + hex of the exact blob bytes."""
    assert (
        manifest_digest(b'{"version": 1}')
        == "sha256:c3aa9744214caf6eb993d6f88f3f1dda3e4e60d59f712b6463c4ce2c4b00dfa1"
    )


def test_digest_empty_blob() -> None:
    """The empty blob digests like any other input (format check)."""
    digest = manifest_digest(b"")
    assert digest.startswith("sha256:")
    assert len(digest) == len("sha256:") + 64
    assert digest == "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def test_digest_differs_on_any_byte_change() -> None:
    """A trailing newline changes the digest — exact bytes are hashed."""
    assert manifest_digest(b'{"version": 1}') != manifest_digest(b'{"version": 1}\n')


def test_parse_minimal_manifest() -> None:
    """A version-only manifest parses with every list empty."""
    manifest = parse_manifest(b'{"version": 1}')
    assert manifest == ProjectManifest(version=1)
    assert required_paths(manifest) == {THIS_REPOSITORY_KEY: []}


def test_parse_full_manifest() -> None:
    """Every field parses and the identity object is preserved."""
    manifest = parse_manifest(
        _blob(
            {
                "version": 1,
                "identity": {"name": "shop", "summary": "storefront"},
                "context": [".agents/project/PROJECT.md"],
                "instructions": ["AGENTS.md"],
                "skills": [".agents/skills"],
                "cross_repo": [{"repository": "docs", "paths": ["api.md"]}],
                "artifacts": ["dist/bundle.js"],
            }
        )
    )
    assert manifest.context == (".agents/project/PROJECT.md",)
    assert manifest.instructions == ("AGENTS.md",)
    assert manifest.skills == (".agents/skills",)
    assert manifest.artifacts == ("dist/bundle.js",)
    assert manifest.cross_repo == (CrossRepoEntry(repository="docs", paths=("api.md",)),)
    assert manifest.identity == {"name": "shop", "summary": "storefront"}


@pytest.mark.parametrize("version", [0, 2, -1, "1", True, None])
def test_version_must_be_1(version: object) -> None:
    """Any version that is not int 1 is rejected (bools included)."""
    payload: dict[str, object] = {}
    if version is not None:
        payload["version"] = version
    with pytest.raises(ManifestError, match="'version'"):
        parse_manifest(_blob(payload))


@pytest.mark.parametrize(
    "path",
    [
        "",
        "/abs/path.md",
        "back\\slash.md",
        "C:/win/path.md",
        "C:relative.md",
        "a//b.md",
        "a/./b.md",
        "../escape.md",
        "a/../../escape.md",
        "dist//",
        ".",
        "..",
    ],
)
@pytest.mark.parametrize("field_name", ["context", "instructions", "skills", "artifacts"])
def test_every_path_rule_rejected_in_each_list_field(field_name: str, path: str) -> None:
    """Each path rule fires in each list field, naming field and path."""
    with pytest.raises(ManifestError, match=field_name):
        parse_manifest(_blob({"version": 1, field_name: [path]}))


@pytest.mark.parametrize(
    "path",
    ["", "/abs.md", "a\\b.md", "C:/x.md", "a//b.md", "./x.md", "../x.md", "x//"],
)
def test_every_path_rule_rejected_in_cross_repo_paths(path: str) -> None:
    """Cross-repo paths obey the same rules, naming the entry."""
    with pytest.raises(ManifestError, match=r"cross_repo\[0\]"):
        parse_manifest(
            _blob({"version": 1, "cross_repo": [{"repository": "docs", "paths": [path]}]})
        )


def test_non_string_path_rejected() -> None:
    """A non-string entry is rejected, not stringified."""
    with pytest.raises(ManifestError, match=r"context\[0\]"):
        parse_manifest(_blob({"version": 1, "context": [42]}))


@pytest.mark.parametrize("field_name", ["context", "instructions", "skills", "artifacts"])
def test_single_trailing_slash_normalised(field_name: str) -> None:
    """A directory-style ``"dist/"`` is accepted as ``"dist"``."""
    manifest = parse_manifest(_blob({"version": 1, field_name: ["dist/"]}))
    assert getattr(manifest, field_name) == ("dist",)


def test_single_trailing_slash_normalised_in_cross_repo() -> None:
    """Cross-repo paths normalise a single trailing slash too."""
    manifest = parse_manifest(
        _blob({"version": 1, "cross_repo": [{"repository": "docs", "paths": ["dist/"]}]})
    )
    assert manifest.cross_repo == (CrossRepoEntry(repository="docs", paths=("dist",)),)


def test_validate_manifest_path() -> None:
    """The shared manifest-path helper accepts relatives, refuses escapes."""
    assert validate_manifest_path(".agents/project/manifest.json") == (
        ".agents/project/manifest.json"
    )
    assert validate_manifest_path("dist/") == "dist"
    for bad in ["", "/abs.md", "../escape.md", "a//b.md", "dist//", "C:/x.md"]:
        with pytest.raises(ManifestError):
            validate_manifest_path(bad)


def test_non_list_field_rejected() -> None:
    """A string where a path list belongs is rejected."""
    with pytest.raises(ManifestError, match="'skills'"):
        parse_manifest(_blob({"version": 1, "skills": ".agents/skills"}))


@pytest.mark.parametrize(
    "cross_repo",
    [
        "docs",
        [{"paths": ["a.md"]}],
        [{"repository": "", "paths": ["a.md"]}],
        [{"repository": 42, "paths": ["a.md"]}],
        [{"repository": "docs"}],
        [{"repository": "docs", "paths": "a.md"}],
        ["just-a-string"],
    ],
)
def test_cross_repo_shape_rejected(cross_repo: object) -> None:
    """Malformed cross_repo entries are rejected naming the entry."""
    with pytest.raises(ManifestError, match="cross_repo"):
        parse_manifest(_blob({"version": 1, "cross_repo": cross_repo}))


def test_identity_must_be_an_object() -> None:
    """A non-object identity is rejected."""
    with pytest.raises(ManifestError, match="'identity'"):
        parse_manifest(_blob({"version": 1, "identity": "shop"}))


def test_identity_id_kept_verbatim() -> None:
    """The optional identity id parses and is kept exactly as given."""
    identity_id = "  Shop Root/AbC-001  "
    manifest = parse_manifest(
        _blob({"version": 1, "identity": {"id": identity_id, "name": "shop"}})
    )
    assert manifest.identity is not None
    assert manifest.identity["id"] == identity_id


@pytest.mark.parametrize("identity_id", [123, "", "   ", None, [], {}])
def test_identity_id_must_be_a_non_empty_string(identity_id: object) -> None:
    """A present identity id must be a non-empty string."""
    with pytest.raises(ManifestError, match=r"identity\.id"):
        parse_manifest(_blob({"version": 1, "identity": {"id": identity_id}}))


@pytest.mark.parametrize("blob", [b"not json", b"[1, 2]", b'"version"', b"1"])
def test_non_object_or_invalid_json_rejected(blob: bytes) -> None:
    """Garbage bytes and non-object JSON never parse."""
    with pytest.raises(ManifestError):
        parse_manifest(blob)


def test_artifacts_not_required() -> None:
    """Artifact paths never appear in required paths."""
    manifest = parse_manifest(
        _blob({"version": 1, "context": ["PROJECT.md"], "artifacts": ["dist/out.js"]})
    )
    assert required_paths(manifest) == {THIS_REPOSITORY_KEY: ["PROJECT.md"]}


def test_required_paths_shape() -> None:
    """Own paths merge in context/instructions/skills order under ``""``."""
    manifest = parse_manifest(
        _blob(
            {
                "version": 1,
                "skills": ["s.md"],
                "context": ["c.md"],
                "instructions": ["i.md"],
                "cross_repo": [
                    {"repository": "docs", "paths": ["a.md", "b.md"]},
                    {"repository": "web", "paths": ["app.ts"]},
                    {"repository": "docs", "paths": ["c.md"]},
                ],
            }
        )
    )
    assert required_paths(manifest) == {
        THIS_REPOSITORY_KEY: ["c.md", "i.md", "s.md"],
        "docs": ["a.md", "b.md", "c.md"],
        "web": ["app.ts"],
    }
