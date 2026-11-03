"""Allow-listed settings files for agent families.

The raw files stay on the host. Only the fields named by the descriptor table,
non-secret account summaries and the selected root may cross the host tunnel.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import stat
import tempfile
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, TypedDict

import json5
import tomlkit
import tomlkit.exceptions
import yaml

from omnigent.config import load_global_config, save_global_config
from omnigent.inner.codex_config_sync import _snapshot

_logger = logging.getLogger(__name__)

JSON_READ_ONLY = "Has comments or isn't plain JSON. Change it by hand."
CHANGED_SINCE = "Changed since"
UNDO_CHANGED = "Changed since. Not undone."
NOT_SIGNED_IN = "Not signed in under this folder"


class FieldView(TypedDict):
    key: str
    present: bool
    value: object
    writable: bool
    undo_available: bool


class AccountSummary(TypedDict):
    kind: str
    label: str


class AgentSettingsView(TypedDict):
    family: str
    root: str
    choosable: bool
    exists: bool
    read_only_reason: str | None
    revision: str | None
    fields: list[FieldView]
    account: AccountSummary | None


class AgentSettingsError(ValueError):
    """A fixed, secret-free refusal suitable for the host frame."""


@dataclass(frozen=True)
class FieldSpec:
    key: str
    writable: bool = True


@dataclass(frozen=True)
class AgentSettingsDescriptor:
    family: str
    env_var: str | None
    default_root: str
    choosable: bool
    filename: str
    format: Literal["json", "toml", "hjson"]
    fields: tuple[FieldSpec, ...]
    has_account_probe: bool


_DESCRIPTORS = (
    AgentSettingsDescriptor(
        "claude",
        "CLAUDE_CONFIG_DIR",
        "~/.claude",
        True,
        "settings.json",
        "json",
        (FieldSpec("model"), FieldSpec("effortLevel"), FieldSpec("permissions.defaultMode")),
        True,
    ),
    AgentSettingsDescriptor(
        "codex",
        "CODEX_HOME",
        "~/.codex",
        True,
        "config.toml",
        "toml",
        (
            FieldSpec("model"),
            FieldSpec("model_reasoning_effort"),
            FieldSpec("approval_policy"),
            FieldSpec("sandbox_mode"),
        ),
        True,
    ),
    AgentSettingsDescriptor(
        "pi",
        "PI_CODING_AGENT_DIR",
        "~/.pi/agent",
        True,
        "settings.json",
        "json",
        (
            FieldSpec("defaultThinkingLevel"),
            FieldSpec("defaultProvider", False),
            FieldSpec("defaultModel", False),
        ),
        True,
    ),
    AgentSettingsDescriptor(
        "agy",
        None,
        "~/.gemini/antigravity-cli",
        False,
        "settings.json",
        "json",
        (FieldSpec("model"), FieldSpec("agentMode"), FieldSpec("toolPermission")),
        False,
    ),
    AgentSettingsDescriptor(
        "agy-acp",
        None,
        "~/.gemini/antigravity-acp",
        False,
        "settings.json",
        "hjson",
        (FieldSpec("auth.type", False),),
        False,
    ),
)
_DESCRIPTOR_BY_FAMILY = {descriptor.family: descriptor for descriptor in _DESCRIPTORS}
_FAMILY_ALIASES = {
    "claude-native": "claude",
    "claude-sdk": "claude",
    "codex-native": "codex",
    "codex-sdk": "codex",
    "pi-native": "pi",
    "pi-rpc": "pi",
    "antigravity-native": "agy",
    "antigravity-acp": "agy-acp",
}


def descriptor_for(family: str) -> AgentSettingsDescriptor:
    canonical = _FAMILY_ALIASES.get(family, family)
    try:
        return _DESCRIPTOR_BY_FAMILY[canonical]
    except KeyError:
        raise AgentSettingsError("Settings folder not known") from None


def resolved_root(family: str) -> str | None:
    """Read only ``harness.<family>.config_root`` from user configuration."""
    descriptor = descriptor_for(family)
    if not descriptor.choosable:
        return None
    try:
        config = load_global_config()
    except (OSError, ValueError, yaml.YAMLError) as exc:
        _logger.warning(
            "Could not read Omnigent config for agent settings: %s", type(exc).__name__
        )
        return None
    if not isinstance(config, Mapping):
        return None
    harness = config.get("harness")
    if not isinstance(harness, Mapping):
        return None
    entry = harness.get(descriptor.family)
    if not isinstance(entry, Mapping):
        return None
    root = entry.get("config_root")
    if not isinstance(root, str) or not root.strip():
        return None
    # Expand a hand-edited ``~`` so launches never receive a literal tilde.
    return os.path.expanduser(root.strip())


def agent_settings_launch_env(family: str, *, root: str | None = None) -> dict[str, str]:
    """Return the configured root's launch override, preserving ambient defaults when unset.

    A family outside the descriptor table (opencode, hermes, a custom ACP
    agent) has no record and no override, so callers that probe every
    installed harness get an empty mapping instead of a refusal.
    """
    try:
        descriptor = descriptor_for(family)
    except AgentSettingsError:
        return {}
    selected = root if root is not None else resolved_root(descriptor.family)
    if descriptor.env_var is None or not selected:
        return {}
    return {descriptor.env_var: selected}


def _root_path(descriptor: AgentSettingsDescriptor, root: str | None) -> Path:
    selected = root or resolved_root(descriptor.family)
    if not selected and descriptor.env_var:
        selected = os.environ.get(descriptor.env_var) or None
    path = Path(selected or descriptor.default_root).expanduser()
    return Path(os.path.abspath(path))


def _revision_id(revision: tuple[int, ...], content: bytes) -> str:
    return hashlib.sha256(repr(revision).encode() + b"\0" + content).hexdigest()


def _optional_snapshot(path: Path) -> tuple[tuple[int, ...], bytes] | None:
    try:
        return _snapshot(path)
    except FileNotFoundError:
        return None


def _reject_json_constant(_value: str) -> object:
    raise ValueError("non-JSON constant")


def _parse(descriptor: AgentSettingsDescriptor, content: bytes) -> MutableMapping[str, object]:
    text = content.decode("utf-8")
    if descriptor.format == "toml":
        return tomlkit.parse(text)
    if descriptor.format == "hjson":
        raw = json5.loads(text)
    else:
        raw = json.loads(text, parse_constant=_reject_json_constant)
    if not isinstance(raw, MutableMapping):
        raise ValueError("settings root is not an object")
    return raw


def _empty_document(descriptor: AgentSettingsDescriptor) -> MutableMapping[str, object]:
    return tomlkit.document() if descriptor.format == "toml" else {}


def _serialize(descriptor: AgentSettingsDescriptor, document: Mapping[str, object]) -> bytes:
    if descriptor.format == "toml":
        return tomlkit.dumps(document).encode("utf-8")
    if descriptor.format == "json":
        return (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    raise AgentSettingsError("This settings file is read-only here.")


def _parts(key: str) -> tuple[str, ...]:
    return tuple(key.split("."))


def _get(document: Mapping[str, object], key: str) -> tuple[bool, object]:
    current: Mapping[str, object] = document
    parts = _parts(key)
    for part in parts[:-1]:
        nested = current.get(part)
        if not isinstance(nested, Mapping):
            return False, None
        current = nested
    return parts[-1] in current, current.get(parts[-1])


def _public_value(value: object) -> object:
    return value if value is None or isinstance(value, (str, int, float, bool)) else None


def _set(document: MutableMapping[str, object], key: str, value: object) -> None:
    current: MutableMapping[str, object] = document
    parts = _parts(key)
    for part in parts[:-1]:
        nested = current.get(part)
        if nested is None:
            nested = tomlkit.table() if isinstance(document, tomlkit.api.TOMLDocument) else {}
            current[part] = nested
        if not isinstance(nested, MutableMapping):
            raise AgentSettingsError("A setting's parent isn't an object. Change it by hand.")
        current = nested
    if value is None:
        current.pop(parts[-1], None)
    else:
        current[parts[-1]] = value


def _undo_path(descriptor: AgentSettingsDescriptor, root: Path) -> Path:
    digest = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:12]
    return Path.home() / ".omnigent" / "agent-settings-undo" / f"{descriptor.family}-{digest}.json"


def _read_undo(path: Path) -> dict[str, dict[str, object]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        _logger.warning("Could not read agent-settings undo state: %s", type(exc).__name__)
        return {}
    if not isinstance(raw, dict):
        return {}
    return {
        key: dict(value)
        for key, value in raw.items()
        if isinstance(key, str) and isinstance(value, dict)
    }


def _atomic_write(
    path: Path,
    content: bytes,
    snapshot: tuple[tuple[int, ...], bytes] | None,
    *,
    mode: int | None = None,
) -> None:
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as handle:
            temporary = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        preserved_mode = stat.S_IMODE(snapshot[0][2]) if snapshot is not None else 0o600
        os.chmod(temporary, mode if mode is not None else preserved_mode)
        if snapshot is None:
            os.link(temporary, path)
        else:
            if _snapshot(path) != snapshot:
                raise AgentSettingsError(CHANGED_SINCE)
            os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _write_undo(path: Path, records: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    content = (json.dumps(records, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    snapshot = _optional_snapshot(path)
    _atomic_write(path, content, snapshot, mode=0o600)


def _account_summary(account: Mapping[str, object] | None) -> AccountSummary | None:
    if account is None:
        return None
    kind = account.get("kind")
    if not isinstance(kind, str):
        return {"kind": "unknown", "label": "Account state unknown"}
    providers = account.get("providers")
    if kind == "none" or (kind == "providers" and providers == []):
        return {"kind": "none", "label": NOT_SIGNED_IN}
    labels = {
        "chatgpt": "ChatGPT",
        "api_key": "API key",
        "subscription": "Subscription",
        "providers": "Signed in",
        "unknown": "Account state unknown",
    }
    label = labels.get(kind, kind)
    for key in ("email", "org", "plan"):
        value = account.get(key)
        if isinstance(value, str) and value:
            label = value
            break
    return {"kind": kind, "label": label}


def _probe_account(descriptor: AgentSettingsDescriptor, root: Path) -> AccountSummary | None:
    if not descriptor.has_account_probe:
        return None
    from omnigent.onboarding.harness_accounts import probe_harness_account

    try:
        return _account_summary(probe_harness_account(descriptor.family, config_root=str(root)))
    except Exception as exc:  # noqa: BLE001 — vendor failures become non-secret metadata
        _logger.warning("Agent account probe failed: %s", type(exc).__name__)
        return {"kind": "unknown", "label": "Account state unknown"}


def _view(
    descriptor: AgentSettingsDescriptor,
    root: Path,
    *,
    account: AccountSummary | None,
) -> AgentSettingsView:
    path = root / descriptor.filename
    snapshot: tuple[tuple[int, ...], bytes] | None = None
    document: Mapping[str, object] = {}
    read_only_reason: str | None = None
    try:
        snapshot = _optional_snapshot(path)
        if snapshot is not None:
            document = _parse(descriptor, snapshot[1])
    except (OSError, UnicodeError, ValueError, tomlkit.exceptions.TOMLKitError) as exc:
        if isinstance(exc, ValueError) and str(exc) == "UNSAFE_FILE":
            read_only_reason = "Symlinked or non-regular files aren't changed here."
        elif descriptor.format == "json":
            read_only_reason = JSON_READ_ONLY
        else:
            read_only_reason = "Couldn't parse this settings file. Change it by hand."
        _logger.warning("Could not read agent settings: %s", type(exc).__name__)
    undo = _read_undo(_undo_path(descriptor, root))
    fields: list[FieldView] = []
    for spec in descriptor.fields:
        present, value = _get(document, spec.key)
        record = undo.get(spec.key)
        fields.append(
            {
                "key": spec.key,
                "present": present,
                "value": _public_value(value) if present else None,
                "writable": spec.writable and read_only_reason is None,
                "undo_available": record is not None,
            }
        )
    return {
        "family": descriptor.family,
        "root": str(root),
        "choosable": descriptor.choosable,
        "exists": snapshot is not None,
        "read_only_reason": read_only_reason,
        "revision": _revision_id(*snapshot) if snapshot is not None else None,
        "fields": fields,
        "account": account,
    }


def read_agent_settings(family: str, root: str | None) -> AgentSettingsView:
    descriptor = descriptor_for(family)
    resolved = _root_path(descriptor, root)
    return _view(descriptor, resolved, account=_probe_account(descriptor, resolved))


def _validate_write_value(value: object) -> None:
    if value is not None and not isinstance(value, (str, int, float, bool)):
        raise AgentSettingsError("Setting values must be strings, numbers, booleans, or null.")


def write_agent_setting(
    family: str,
    root: str | None,
    key: str,
    value: object,
    revision: str | None,
) -> AgentSettingsView:
    descriptor = descriptor_for(family)
    spec = next((field for field in descriptor.fields if field.key == key), None)
    if spec is None or not spec.writable:
        raise AgentSettingsError("This setting isn't writable here.")
    _validate_write_value(value)
    resolved = _root_path(descriptor, root)
    if not resolved.is_dir():
        raise AgentSettingsError("Choose an existing settings folder first.")
    path = resolved / descriptor.filename
    try:
        snapshot = _optional_snapshot(path)
        current_revision = _revision_id(*snapshot) if snapshot is not None else None
        if revision != current_revision:
            raise AgentSettingsError(CHANGED_SINCE)
        document = (
            _parse(descriptor, snapshot[1])
            if snapshot is not None
            else _empty_document(descriptor)
        )
        present, previous = _get(document, key)
        undo_path = _undo_path(descriptor, resolved)
        records = _read_undo(undo_path)
        records[key] = {
            "present": present,
            "value": _public_value(previous),
            "after": value,
            "revision": current_revision,
            "at": datetime.now(UTC).isoformat(),
        }
        _write_undo(undo_path, records)
        _set(document, key, value)
        _atomic_write(path, _serialize(descriptor, document), snapshot)
        fresh = _optional_snapshot(path)
        if fresh is not None:
            records = _read_undo(undo_path)
            if key in records:
                records[key]["revision"] = _revision_id(*fresh)
                _write_undo(undo_path, records)
    except AgentSettingsError:
        raise
    except (OSError, UnicodeError, ValueError, tomlkit.exceptions.TOMLKitError) as exc:
        _logger.warning("Could not write agent setting: %s", type(exc).__name__)
        raise AgentSettingsError("Couldn't write this settings file. Change it by hand.") from None
    return _view(descriptor, resolved, account=_probe_account(descriptor, resolved))


def undo_agent_setting(family: str, root: str | None, key: str) -> AgentSettingsView:
    descriptor = descriptor_for(family)
    if not any(field.key == key and field.writable for field in descriptor.fields):
        raise AgentSettingsError("This setting isn't writable here.")
    resolved = _root_path(descriptor, root)
    path = resolved / descriptor.filename
    undo_path = _undo_path(descriptor, resolved)
    records = _read_undo(undo_path)
    record = records.get(key)
    if record is None:
        raise AgentSettingsError("Nothing to undo.")
    try:
        snapshot = _optional_snapshot(path)
        if snapshot is None:
            raise AgentSettingsError(UNDO_CHANGED)
        document = _parse(descriptor, snapshot[1])
        present, current = _get(document, key)
        after = record.get("after")
        if (after is None and present) or (
            after is not None and (not present or current != after)
        ):
            raise AgentSettingsError(UNDO_CHANGED)
        _set(document, key, record.get("value") if record.get("present") is True else None)
        _atomic_write(path, _serialize(descriptor, document), snapshot)
        records.pop(key, None)
        _write_undo(undo_path, records)
    except AgentSettingsError:
        raise
    except (OSError, UnicodeError, ValueError, tomlkit.exceptions.TOMLKitError) as exc:
        _logger.warning("Could not undo agent setting: %s", type(exc).__name__)
        raise AgentSettingsError(UNDO_CHANGED) from None
    return _view(descriptor, resolved, account=_probe_account(descriptor, resolved))


def agent_settings_set_root(family: str, root: str | None) -> AgentSettingsView:
    descriptor = descriptor_for(family)
    if not descriptor.choosable:
        raise AgentSettingsError("This settings folder is fixed by the agent.")
    selected: Path | None = None
    if root and root.strip():
        selected = Path(root).expanduser()
        if not selected.is_absolute():
            raise AgentSettingsError("Choose an absolute settings folder.")
        selected = Path(os.path.abspath(selected))
        if not selected.is_dir():
            raise AgentSettingsError("Choose an existing settings folder.")
    config = load_global_config()
    if not isinstance(config, Mapping):
        raise AgentSettingsError("Couldn't update the settings folder.")
    raw_harness = config.get("harness")
    harness: dict[str, object]
    if isinstance(raw_harness, Mapping):
        harness = dict(raw_harness)
    elif isinstance(raw_harness, str):
        harness = {"default": raw_harness}
    else:
        harness = {}
    entry_value = harness.get(descriptor.family)
    entry = dict(entry_value) if isinstance(entry_value, Mapping) else {}
    if selected is None:
        entry.pop("config_root", None)
    else:
        entry["config_root"] = str(selected)
    if entry:
        harness[descriptor.family] = entry
    else:
        harness.pop(descriptor.family, None)
    save_global_config({"harness": harness})
    return read_agent_settings(descriptor.family, str(selected) if selected is not None else None)


__all__ = [
    "AgentSettingsDescriptor",
    "AgentSettingsError",
    "AgentSettingsView",
    "FieldSpec",
    "agent_settings_launch_env",
    "agent_settings_set_root",
    "descriptor_for",
    "read_agent_settings",
    "resolved_root",
    "undo_agent_setting",
    "write_agent_setting",
]
