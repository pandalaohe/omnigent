"""Server-wide provider entries; credentials resolve only at the call boundary."""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from omnigent.onboarding import secrets
from omnigent.onboarding.provider_config import load_providers
from omnigent.server.admin_list import resolve_data_dir

_LOCK = threading.Lock()


class ConnectionConflict(ValueError):
    """A provider or row is still referenced, or a create id already exists."""

    def __init__(self, detail: dict):
        self.detail = detail
        super().__init__("Provider conflict")


class ConnectionUnavailable(ValueError):
    """A selected provider or its credential is no longer available."""


class KeyValue(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    value: SecretStr = Field(min_length=1, max_length=4096)


class KeyEnvironment(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    env: str = Field(pattern=r"^[A-Z_][A-Z0-9_]{0,63}$")


class ConnectionWrite(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    display_name: str = Field(default="", max_length=100)
    kind: Literal["key", "gateway", "local"] = "key"
    base_url: str = Field(max_length=2048)
    wire_api: Literal["chat", "responses"] = "chat"
    models: dict[str, str] = Field(default_factory=dict, max_length=50)
    model_display_names: dict[str, str] = Field(default_factory=dict, max_length=50)
    key: KeyValue | KeyEnvironment | None = Field(default=None, repr=False)

    @field_validator("base_url")
    @classmethod
    def literal_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or any(c in value for c in "$ {}\r\n\t?#")
        ):
            raise ValueError("Invalid endpoint")
        _ = parsed.port
        return value.rstrip("/")

    @field_validator("models")
    @classmethod
    def model_lengths(cls, value: dict[str, str]) -> dict[str, str]:
        if any(
            not role or len(role) > 200 or not model or len(model) > 200
            for role, model in value.items()
        ):
            raise ValueError("Invalid models")
        return value

    @field_validator("model_display_names")
    @classmethod
    def display_lengths(cls, value: dict[str, str]) -> dict[str, str]:
        if any(len(key) > 200 or len(name) > 100 for key, name in value.items()):
            raise ValueError("Invalid model display names")
        return value


@dataclass(frozen=True)
class ResolvedConnection:
    base_url: str
    api_key: str = field(repr=False)
    wire_api: str = "chat"
    model_ids: tuple[str, ...] = ()
    kind: str = "key"


def validate_name(name: str) -> None:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,47}", name) or name == "server-llm":
        raise ValueError("Invalid provider name")


def _load() -> dict:
    path = resolve_data_dir() / "model_connections.json"
    try:
        return json.loads(path.read_text())["providers"]
    except FileNotFoundError:
        return {}


def _write(entries: dict) -> None:
    root = resolve_data_dir()
    root.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=root, prefix=".model-connections-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump({"providers": entries}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, root / "model_connections.json")
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _entry(name: str, draft: ConnectionWrite, previous: dict | None = None) -> dict:
    validate_name(name)
    reference = (previous or {}).get("openai", {}).get("api_key_ref")
    if isinstance(draft.key, KeyValue):
        reference = f"keychain:server-connection:{name}"
    elif isinstance(draft.key, KeyEnvironment):
        reference = f"env:{draft.key.env}"
    if not reference:
        raise ValueError("A key source is required")
    entry: dict = {
        "kind": draft.kind,
        "display_name": draft.display_name or name,
        "model_display_names": {
            key: draft.model_display_names.get(key, "") for key in draft.models
        },
        "openai": {
            "base_url": draft.base_url,
            "wire_api": draft.wire_api,
            "api_key_ref": reference,
            "models": draft.models,
        },
    }
    for field_name in ("model_catalog", "model_seen", "model_previous_ids"):
        if previous and field_name in previous:
            entry[field_name] = previous[field_name]
    for field_name in ("model_seen", "model_previous_ids"):
        entry[field_name] = {
            key: value for key, value in entry.get(field_name, {}).items() if key in draft.models
        }
    load_providers({"providers": {name: entry}})
    return entry


def _resolve(entry: dict) -> ResolvedConnection:
    family = entry["openai"]
    reference = family["api_key_ref"]
    if reference.startswith("env:"):
        key = os.environ.get(reference[4:])
    else:
        key = secrets.load_secret(reference[9:], home=str(resolve_data_dir()))
    if not key:
        raise ConnectionUnavailable("Provider key unavailable")
    return ResolvedConnection(
        family["base_url"],
        key,
        family["wire_api"],
        tuple(family.get("models", {}).values()),
        entry["kind"],
    )


def resolve_connection(name: str) -> ResolvedConnection:
    entry = _load().get(name)
    if entry is None:
        raise ConnectionUnavailable("Provider unavailable")
    try:
        return _resolve(entry)
    except Exception:  # noqa: BLE001 - redact provider and credential failures
        raise ConnectionUnavailable("Provider key unavailable") from None


def resolve_reference(
    connection: str, row_key: str, legacy_id: str, *, entries: dict | None = None
) -> tuple[str | None, str, bool]:
    if connection == "server-llm":
        from omnigent.runtime import get_caps

        llm = get_caps().llm
        if row_key == "config" and llm is not None:
            return "config", llm.model, False
        if not row_key and llm is not None and legacy_id == llm.model:
            return "config", llm.model, False
        return None, legacy_id, bool(row_key or legacy_id)
    entry = (entries if entries is not None else _load()).get(connection, {})
    models = entry.get("openai", {}).get("models", {})
    if row_key:
        return (row_key, models[row_key], False) if row_key in models else (None, legacy_id, True)
    for key, model in models.items():
        if model == legacy_id:
            return key, model, False
    for key, old in entry.get("model_previous_ids", {}).items():
        if key in models and legacy_id in old:
            return key, models[key], False
    return None, legacy_id, bool(legacy_id)


def _voice_slots(value: dict):
    if value.get("speech_source") == "third":
        yield (
            "Speech to text",
            value.get("speech_connection", ""),
            value.get("speech_row", ""),
            value.get("speech_model", ""),
        )
    one_step = value.get("speech_source") == "third" and value.get("speech_method") == "chat_audio"
    if not one_step and value.get("mode", "automatic") != "off" and value.get("model"):
        yield (
            "Cleanup",
            value.get("cleanup_connection", "server-llm"),
            value.get("cleanup_row", ""),
            value["model"],
        )


def references(store, *, entries: dict | None = None) -> dict:
    entries = _load() if entries is None else entries
    providers: dict[str, set[str]] = {}
    slots: dict[tuple[str, str], dict[str, set[str]]] = {}
    legacy: dict[str, set[str]] = {}
    for user, value in store.namespace_values("voice_input") if store is not None else []:
        if not isinstance(value, dict):
            continue
        for provider, row, model in (
            (
                value.get("speech_connection", ""),
                value.get("speech_row", ""),
                value.get("speech_model", ""),
            ),
            (
                value.get("cleanup_connection", "server-llm"),
                value.get("cleanup_row", ""),
                value.get("model", ""),
            ),
        ):
            if not row and model:
                legacy.setdefault(provider, set()).add(model)
        for slot, provider, row, model in _voice_slots(value):
            providers.setdefault(provider, set()).add(user)
            key, _, _ = resolve_reference(provider, row, model, entries=entries)
            if key:
                slots.setdefault((provider, key), {}).setdefault(slot, set()).add(user)
    return {
        "providers": {provider: len(users) for provider, users in providers.items()},
        "rows": {
            key: {
                "users": len(set().union(*values.values())),
                "slots": [
                    {"feature": "Voice input", "slot": slot, "users": len(users)}
                    for slot, users in values.items()
                ],
            }
            for key, values in slots.items()
        },
        "legacy": legacy,
    }


def _status(entry: dict, key: str, model: str) -> str:
    cache = entry.get("model_catalog")
    if not cache:
        return "unchecked"
    if model in cache.get("ids", []):
        return "listed"
    if not cache.get("complete", False):
        return "unchecked"
    return "gone" if entry.get("model_seen", {}).get(key) == model else "not_listed"


def _row(name: str, entry: dict, admin: bool, usage: dict | None = None) -> dict:
    family = entry["openai"]
    reference = family["api_key_ref"]
    try:
        key_set = bool(_resolve(entry).api_key)
    except Exception:  # noqa: BLE001 - redact provider and credential failures
        key_set = False
    row = {
        "name": name,
        "display_name": entry.get("display_name", name),
        "kind": entry["kind"],
        "base_url": family["base_url"],
        "wire_api": family["wire_api"],
        "models": [
            {
                "key": key,
                "display_name": entry.get("model_display_names", {}).get(key) or model,
                "model_id": model,
                "status": _status(entry, key, model),
                **(
                    {
                        "used_by": (usage or {})
                        .get("rows", {})
                        .get((name, key), {"users": 0, "slots": []})
                    }
                    if admin
                    else {}
                ),
            }
            for key, model in family.get("models", {}).items()
        ],
        "key_set": key_set,
        "key_source": "env" if reference.startswith("env:") else "stored",
        "editable": True,
        "model_previous_ids": entry.get("model_previous_ids", {}),
    }
    if admin and reference.startswith("env:"):
        row["key_env"] = reference[4:]
    if admin:
        row["catalog"] = entry.get("model_catalog")
        row["used_by"] = (usage or {}).get("providers", {}).get(name, 0)
    return row


def load_connections(*, admin: bool = False, store=None) -> list[dict]:
    from omnigent.runtime import get_caps

    entries = _load()
    usage = references(store, entries=entries) if admin else None
    rows = [_row(name, entry, admin, usage) for name, entry in entries.items()]
    llm = get_caps().llm
    if llm is not None:
        # Configuration may contain credentials; expose only the URL hostname.
        connection = llm.connection or {}
        address = connection.get("base_url", "")
        rows.insert(
            0,
            {
                "name": "server-llm",
                "display_name": "Server LLM",
                "kind": "key",
                "base_url": urlsplit(address).hostname or "provider default",
                "wire_api": "responses",
                "models": [
                    {
                        "key": "config",
                        "display_name": llm.model,
                        "model_id": llm.model,
                        "status": "config",
                        **(
                            {
                                "used_by": (usage or {})
                                .get("rows", {})
                                .get(("server-llm", "config"), {"users": 0, "slots": []})
                            }
                            if admin
                            else {}
                        ),
                    }
                ],
                "key_set": bool(connection.get("api_key") or connection.get("profile")),
                "key_source": "config-file",
                "editable": False,
            },
        )
    return rows


def upsert_connection(
    name: str, draft: ConnectionWrite, *, store=None, create: bool = False
) -> dict:
    with _LOCK:
        entries = _load()
        if create and name in entries:
            raise ConnectionConflict({"kind": "exists"})
        usage = references(store, entries=entries)
        blocked = {
            key: value
            for (provider, key), value in usage["rows"].items()
            if provider == name and key not in draft.models
        }
        if blocked:
            raise ConnectionConflict({"kind": "in_use", "rows": blocked})
        entry = _entry(name, draft, entries.get(name))
        previous_models = entries.get(name, {}).get("openai", {}).get("models", {})
        for key, model in draft.models.items():
            history = entry["model_previous_ids"].get(key, [])
            old = previous_models.get(key)
            if old and old != model:
                history = [*history, old]
            entry["model_previous_ids"][key] = list(
                dict.fromkeys(
                    value for value in history if value in usage["legacy"].get(name, set())
                )
            )[-5:]
        if isinstance(draft.key, KeyValue):
            secrets.store_secret(
                f"server-connection:{name}",
                draft.key.value.get_secret_value(),
                home=str(resolve_data_dir()),
            )
        entries[name] = entry
        _write(entries)
        return _row(name, entry, True, usage)


def delete_connection(name: str, *, store=None) -> None:
    validate_name(name)
    with _LOCK:
        entries = _load()
        users = references(store, entries=entries)["providers"].get(name, 0)
        if users:
            raise ConnectionConflict({"kind": "in_use", "users": users})
        entries.pop(name, None)
        _write(entries)
        secrets.delete_secret(f"server-connection:{name}", home=str(resolve_data_dir()))


def probe_connection(name: str, draft: ConnectionWrite, *, full: bool = False) -> dict:
    entry = _entry(name, draft, _load().get(name))
    try:
        if isinstance(draft.key, KeyValue):
            key = draft.key.value.get_secret_value()
        else:
            key = _resolve(entry).api_key
        with httpx.Client(follow_redirects=False, timeout=10) as client:
            response = client.get(
                draft.base_url + "/models", headers={"Authorization": f"Bearer {key}"}
            )
        if response.is_redirect:
            error = "redirect"
        elif response.status_code in (401, 403):
            error = "auth"
        elif not response.is_success:
            error = f"http_{response.status_code}"
        else:
            data = response.json()["data"]
            if not isinstance(data, list):
                raise ValueError("Invalid catalogue")
            ids = [
                item["id"]
                for item in data
                if isinstance(item, dict)
                and isinstance(item.get("id"), str)
                and 0 < len(item["id"]) <= 200
            ]
            ids = list(dict.fromkeys(ids))
            if not full:
                ids = ids[:200]
            return {"ok": True, "model_ids": ids, "error_kind": None}
    except httpx.TimeoutException:
        error = "timeout"
    except httpx.RequestError:
        error = "network"
    except ConnectionUnavailable:
        error = "auth"
    except (ValueError, KeyError, TypeError):
        error = "bad_response"
    return {"ok": False, "model_ids": [], "error_kind": error}


def catalog(name: str) -> dict:
    cache = _load().get(name, {}).get("model_catalog", {})
    return {"fetched_at": cache.get("fetched_at"), "ids": cache.get("ids", [])}


def refresh_catalog(name: str, *, store=None) -> dict:
    entry = _load().get(name)
    if entry is None:
        raise ConnectionUnavailable("Provider unavailable")
    family = entry["openai"]
    draft = ConnectionWrite(base_url=family["base_url"], models=family.get("models", {}))
    result = probe_connection(name, draft, full=True)
    if not result["ok"]:
        return result
    ids = result["model_ids"]
    with _LOCK:
        entries = _load()
        current = entries.get(name)
        if current is None or current["openai"] != family:
            raise ConnectionConflict({"kind": "changed"})
        current["model_catalog"] = {
            "fetched_at": datetime.now(UTC).isoformat(),
            "ids": ids[:2000],
            "complete": len(ids) <= 2000,
        }
        seen = current.setdefault("model_seen", {})
        membership = set(ids)
        for key, model in family.get("models", {}).items():
            if model in membership:
                seen[key] = model
        _write(entries)
        return _row(name, current, True, references(store, entries=entries))
