"""Which paths an artifact bundle may serve.

A bundle token promises the entry's folder and its descendants and nothing
outside it, but lexical checks alone cannot keep that promise: a symlink
inside the folder can point anywhere. Callers therefore canonicalize both
the target and the bundle root first and apply these rules to the real
path, so a symlink cannot widen the bundle.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Sequence
from pathlib import Path

# Bundle sub-resource names that convention reserves for key material.
_FORBIDDEN_NAME_PATTERNS = (
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.env",
    "id_rsa*",
    "id_ecdsa*",
    "id_ed25519*",
)


def artifact_segments_valid(segments: Sequence[str]) -> bool:
    """Whether *segments* is a non-empty path with no odd components.

    :param segments: Path components, e.g. ``("reports", "index.html")``.
    :returns: ``False`` for an empty path, or for any empty, ``"."`` or
        ``".."`` component, or a component containing ``"\\"`` or NUL.
    """
    if not segments:
        return False
    return all(
        bool(segment)
        and segment not in (".", "..")
        and "\\" not in segment
        and "\x00" not in segment
        for segment in segments
    )


def artifact_path_allowed(segments: Sequence[str]) -> bool:
    """Whether the bundle sub-resource rules admit a resolved relative path.

    :param segments: Path components relative to the bundle root, e.g.
        ``("assets", "style.css")``.
    :returns: ``True`` when :func:`artifact_segments_valid` passes, no
        component is a dotfile or dot-directory, and the final component
        does not match a key-material name pattern (case-insensitively).
    """
    if not artifact_segments_valid(segments):
        return False
    if any(segment.startswith(".") for segment in segments):
        return False
    last = segments[-1]
    return not any(
        fnmatch.fnmatchcase(last.lower(), pattern) for pattern in _FORBIDDEN_NAME_PATTERNS
    )


def resolved_within(resolved: Path, root: Path) -> bool:
    """Whether an already-resolved path is a strictly-inside allowed descendant.

    :param resolved: Canonical (realpath) path of the target.
    :param root: Canonical (realpath) path of the bundle root.
    :returns: ``True`` when *resolved* is strictly inside *root* and its
        relative path passes :func:`artifact_path_allowed`.
    """
    try:
        relative = resolved.relative_to(root)
    except ValueError:
        return False
    return artifact_path_allowed(relative.parts)
