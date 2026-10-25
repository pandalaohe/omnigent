"""Refresh private Codex permission defaults and digest-pinned policy leaves."""

from __future__ import annotations

import hashlib
import logging
import os
import re
import stat
import tempfile
from collections.abc import MutableMapping
from pathlib import Path
from typing import Literal

import tomlkit
import tomlkit.exceptions
import tomllib
import yaml

from omnigent.config import load_global_config

_logger = logging.getLogger(__name__)
_LEAF = "auto_review.extra_policy"
_STANCE_KEYS = ("approval_policy", "sandbox_mode", "approvals_reviewer", "default_permissions")
SyncResult = Literal["created", "unchanged", "updated", "conflict"]


def _revision(info: os.stat_result) -> tuple[int, ...]:
    return info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _snapshot(path: Path) -> tuple[tuple[int, ...], bytes]:
    """Read a regular, non-linked file and reject changes during the read."""
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise ValueError("UNSAFE_FILE")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        handle = os.fdopen(fd, "rb")
    except OSError:
        os.close(fd)
        raise
    with handle:
        opened = os.fstat(handle.fileno())
        if _revision(before) != _revision(opened):
            raise ValueError("CONCURRENT_CHANGE")
        content = handle.read()
        after = os.fstat(handle.fileno())
    if _revision(opened) != _revision(after) or _revision(after) != _revision(path.lstat()):
        raise ValueError("CONCURRENT_CHANGE")
    return _revision(after), content


def _optional_snapshot(path: Path) -> tuple[tuple[int, ...], bytes] | None:
    try:
        return _snapshot(path)
    except FileNotFoundError:
        return None


def _log_conflict(exc: Exception) -> None:
    # Parser/I/O errors can contain provider values; expose only their type.
    reason = str(exc) if type(exc) is ValueError else type(exc).__name__
    _logger.warning("Codex config sync conflict: %s; skipping refresh", reason)


def _sync_pinned_policy(source_data: dict, document: MutableMapping) -> None:
    """Validate the optional leaf completely before changing the document."""
    config = load_global_config()
    if not isinstance(config, dict):
        raise ValueError("INVALID_SYNC_CONFIG")
    pins = config.get("codex_config_sync", {})
    if pins is None or pins == {}:
        return
    if (
        not isinstance(pins, dict)
        or set(pins) != {_LEAF}
        or not isinstance(pins[_LEAF], str)
        or re.fullmatch(r"[0-9a-fA-F]{64}", pins[_LEAF]) is None
    ):
        raise ValueError("INVALID_SYNC_CONFIG")
    source_review = source_data.get("auto_review")
    if not isinstance(source_review, dict) or "extra_policy" not in source_review:
        raise ValueError("SOURCE_POLICY_MISSING")
    value = source_review["extra_policy"]
    if "policy" in source_review:
        raise ValueError("SOURCE_REPLACEMENT_POLICY")
    if (
        not isinstance(value, str)
        or hashlib.sha256(value.encode("utf-8")).hexdigest() != pins[_LEAF].lower()
    ):
        raise ValueError("SOURCE_POLICY_MISMATCH")
    review = document.get("auto_review")
    if review is not None and not isinstance(review, MutableMapping):
        raise ValueError("TARGET_POLICY_CONFLICT")
    if review is not None:
        if "policy" in review:
            raise ValueError("TARGET_REPLACEMENT_POLICY")
        if "extra_policy" in review and review["extra_policy"] != value:
            raise ValueError("TARGET_POLICY_CONFLICT")
    if review is None:
        review = tomlkit.table()
        document["auto_review"] = review
    if "extra_policy" not in review:
        review["extra_policy"] = value


def sync_codex_config(
    source: Path,
    target: Path,
    *,
    minimal_config: bool,
    include_credentials: bool,
) -> SyncResult | None:
    """Refresh permission defaults and apply user-level policy digest pins.

    Permission defaults mirror the source, including removal of absent keys.
    ``auto_review.extra_policy`` additionally requires a digest pin. Minimal
    probes/titles skip refresh; signer-backed user sessions receive these
    settings independently of host provider credentials.
    Policy conflicts skip that leaf while permission defaults still refresh.
    Unsafe files and concurrent changes skip publication without blocking startup.
    Sync diagnostics never log configuration contents.
    """
    if minimal_config and include_credentials:
        return None
    try:
        source_snapshot = _optional_snapshot(source)
        target_snapshot = _optional_snapshot(target)
        if source_snapshot is None and target_snapshot is None:
            return None
        source_data = tomllib.loads(source_snapshot[1].decode("utf-8")) if source_snapshot else {}
        if target_snapshot is None and include_credentials and source_snapshot:
            # Ordinary fresh homes retain the complete-copy contract.
            text = source_snapshot[1].decode("utf-8")
        else:
            text = target_snapshot[1].decode("utf-8") if target_snapshot else ""
        document = tomlkit.parse(text)
        for key in _STANCE_KEYS:
            if key in source_data:
                if document.get(key) != source_data[key]:
                    document[key] = source_data[key]
            else:
                document.pop(key, None)
        policy_conflict = False
        try:
            _sync_pinned_policy(source_data, document)
        except (OSError, ValueError, yaml.YAMLError) as exc:
            _log_conflict(exc)
            policy_conflict = True
        output = tomlkit.dumps(document).encode("utf-8")
        if target_snapshot is not None and output == target_snapshot[1]:
            return "conflict" if policy_conflict else "unchanged"
        # Validate the complete candidate before publishing any bytes.
        tomllib.loads(output.decode("utf-8"))
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=target.parent, prefix="config.toml.", delete=False
            ) as handle:
                temporary = Path(handle.name)
                handle.write(output)
                handle.flush()
                os.fsync(handle.fileno())
            if _optional_snapshot(source) != source_snapshot:
                raise ValueError("CONCURRENT_SOURCE_CHANGE")
            if target_snapshot is None:
                # Unlike replace, link refuses a target created concurrently.
                os.link(temporary, target)
            else:
                if _snapshot(target) != target_snapshot:
                    raise ValueError("CONCURRENT_TARGET_CHANGE")
                os.replace(temporary, target)
            _logger.info(
                "Codex config sync: %s", "created" if target_snapshot is None else "updated"
            )
            return "created" if target_snapshot is None else "updated"
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    except (
        OSError,
        UnicodeError,
        ValueError,
        tomlkit.exceptions.TOMLKitError,
        yaml.YAMLError,
    ) as exc:
        _log_conflict(exc)
        return "conflict"
