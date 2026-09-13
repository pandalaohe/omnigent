"""Project-context manifest parsing and digest.

A registered repository carries ``.agents/project/manifest.json`` declaring
which committed paths a receiving host must find at pickup. The dispatch
tool hashes the manifest blob at the pinned commit and the receiving host
re-hashes it before preparing the worktree, so a wrong dispatch is refused
instead of starting blind.

This module is pure: no git, no I/O beyond its arguments.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

#: Manifest schema version this code reads. ``version`` must equal this.
MANIFEST_VERSION = 1

#: Key in :func:`required_paths` holding the paths required in the
#: manifest's own repository. The empty string can never be a repository
#: name, so it cannot collide with a ``cross_repo`` key.
THIS_REPOSITORY_KEY = ""

_DRIVE_LETTER_RE = re.compile(r"^[A-Za-z]:")


class ManifestError(ValueError):
    """Raised when a manifest blob fails validation.

    The message always names the offending field or path, e.g.
    ``"manifest field 'context[1]' has an invalid path '../x': ..."``.
    """


@dataclass(frozen=True)
class CrossRepoEntry:
    """One ``cross_repo`` entry: required paths in a sibling repository.

    :param repository: Registered repository name, e.g. ``"docs"``.
    :param paths: Repo-relative paths required in that repository.
    """

    repository: str
    paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProjectManifest:
    """A parsed project-context manifest.

    :param version: Always :data:`MANIFEST_VERSION`.
    :param context: Declarative context paths in this repository.
    :param instructions: Instruction paths in this repository.
    :param skills: Skill paths in this repository.
    :param artifacts: Output paths; declared but never required at pickup.
    :param cross_repo: Required paths in sibling repositories.
    :param identity: The manifest's ``identity`` object as a read-only
        view, or ``None`` when absent.
    """

    version: int = MANIFEST_VERSION
    context: tuple[str, ...] = ()
    instructions: tuple[str, ...] = ()
    skills: tuple[str, ...] = ()
    artifacts: tuple[str, ...] = ()
    cross_repo: tuple[CrossRepoEntry, ...] = ()
    identity: Mapping[str, Any] | None = field(default=None)


def manifest_digest(blob: bytes) -> str:
    """Hash a manifest blob into its pinned digest.

    The ``sha256:`` + lowercase-hex-of-exact-bytes form is shared with the
    dispatch tool: both sides hash the raw blob bytes at the pinned commit,
    so any byte difference (including line endings) is a mismatch.

    :param blob: Exact manifest blob bytes at the pinned commit.
    :returns: E.g. ``"sha256:4bf5..."`` (64 hex chars).
    """
    return f"sha256:{hashlib.sha256(blob).hexdigest()}"


def _validate_path(label: str, value: object) -> str:
    """Validate one repo-relative manifest path.

    :param label: Field label for errors, e.g. ``"context[1]"``.
    :param value: The raw path value.
    :returns: The validated path, with a single trailing ``/`` normalised away.
    :raises ManifestError: Naming ``label`` and the offending value.
    """
    if not isinstance(value, str) or not value:
        raise ManifestError(f"manifest field {label!r} must be a non-empty path")
    # A single trailing "/" marks a directory (e.g. "dist/"); normalise it
    # away. Anything else with an empty segment ("//") still fails below.
    if value.endswith("/") and not value.endswith("//"):
        value = value[:-1]
    if value.startswith("/"):
        raise ManifestError(
            f"manifest field {label!r} has an invalid path {value!r}: "
            "must be repo-relative, not start with '/'"
        )
    if "\\" in value:
        raise ManifestError(
            f"manifest field {label!r} has an invalid path {value!r}: "
            "must use '/' separators, not '\\'"
        )
    if _DRIVE_LETTER_RE.match(value):
        raise ManifestError(
            f"manifest field {label!r} has an invalid path {value!r}: "
            "must not carry a drive letter"
        )
    for segment in value.split("/"):
        if not segment:
            raise ManifestError(
                f"manifest field {label!r} has an invalid path {value!r}: "
                "must not contain an empty segment"
            )
        if segment in (".", ".."):
            raise ManifestError(
                f"manifest field {label!r} has an invalid path {value!r}: "
                f"must not contain a {segment!r} segment"
            )
    return value


def validate_manifest_path(value: object) -> str:
    """Validate a repo-relative manifest path such as ``context_manifest_path``.

    Same rules as manifest entries (a single trailing ``/`` is normalised
    away). The host reuses this before interpolating the path into
    ``<commit>:<path>`` so an untrusted value cannot escape the repository.

    :param value: The raw path value.
    :returns: The validated (normalised) path.
    :raises ManifestError: Naming the offending value.
    """
    return _validate_path("context_manifest_path", value)


def _path_list(raw: object, field_name: str) -> tuple[str, ...]:
    """Validate an optional list-of-paths manifest field.

    :param raw: The raw field value (absent is ``None``).
    :param field_name: Field name for errors, e.g. ``"context"``.
    :returns: Validated paths, empty when the field is absent.
    :raises ManifestError: Naming the field and the offending entry.
    """
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ManifestError(f"manifest field {field_name!r} must be a list of paths")
    return tuple(_validate_path(f"{field_name}[{i}]", item) for i, item in enumerate(raw))


def _cross_repo(raw: object) -> tuple[CrossRepoEntry, ...]:
    """Validate the optional ``cross_repo`` manifest field.

    :param raw: The raw field value (absent is ``None``).
    :returns: Validated entries, empty when the field is absent.
    :raises ManifestError: Naming the field and the offending entry.
    """
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ManifestError("manifest field 'cross_repo' must be a list")
    entries: list[CrossRepoEntry] = []
    for i, item in enumerate(raw):
        label = f"cross_repo[{i}]"
        if not isinstance(item, dict):
            raise ManifestError(f"manifest field {label!r} must be an object")
        repository = item.get("repository")
        if not isinstance(repository, str) or not repository:
            raise ManifestError(f"manifest field {label!r} must name a non-empty 'repository'")
        paths = item.get("paths")
        if not isinstance(paths, list):
            raise ManifestError(f"manifest field {label!r} must carry a 'paths' list")
        entries.append(
            CrossRepoEntry(
                repository=repository,
                paths=tuple(
                    _validate_path(f"{label}.paths[{j}]", path) for j, path in enumerate(paths)
                ),
            )
        )
    return tuple(entries)


def parse_manifest(blob: bytes) -> ProjectManifest:
    """Parse and validate a manifest blob.

    :param blob: Raw ``manifest.json`` bytes at the pinned commit.
    :returns: The validated manifest.
    :raises ManifestError: Naming the offending field or path.
    """
    try:
        raw = json.loads(blob)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ManifestError(f"manifest is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ManifestError("manifest must be a JSON object")
    version = raw.get("version")
    if isinstance(version, bool) or version != MANIFEST_VERSION:
        raise ManifestError(
            f"manifest field 'version' must be {MANIFEST_VERSION}, got {version!r}"
        )
    identity = raw.get("identity")
    if identity is not None and not isinstance(identity, dict):
        raise ManifestError("manifest field 'identity' must be an object")
    return ProjectManifest(
        version=MANIFEST_VERSION,
        context=_path_list(raw.get("context"), "context"),
        instructions=_path_list(raw.get("instructions"), "instructions"),
        skills=_path_list(raw.get("skills"), "skills"),
        artifacts=_path_list(raw.get("artifacts"), "artifacts"),
        cross_repo=_cross_repo(raw.get("cross_repo")),
        identity=MappingProxyType(dict(identity)) if identity is not None else None,
    )


def required_paths(manifest: ProjectManifest) -> dict[str, list[str]]:
    """Map each repository to the paths required in it at pickup.

    The manifest's own repository is keyed by :data:`THIS_REPOSITORY_KEY`;
    every other key is a ``cross_repo`` repository name (duplicate entries
    for one repository merge in order). ``artifacts`` are outputs and are
    never required. A ``cross_repo`` repository unknown to the assignment
    is a manifest error for the caller to raise naming it — this function
    only groups, it does not know the assignment's repository set.

    :param manifest: A parsed manifest.
    :returns: E.g. ``{"": ["AGENTS.md"], "docs": ["api.md"]}``.
    """
    required: dict[str, list[str]] = {
        THIS_REPOSITORY_KEY: [
            *manifest.context,
            *manifest.instructions,
            *manifest.skills,
        ]
    }
    for entry in manifest.cross_repo:
        required.setdefault(entry.repository, []).extend(entry.paths)
    return required
