"""Server-wide provider entries; credentials resolve only at the call boundary."""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from omnigent.onboarding import secrets
from omnigent.onboarding.provider_config import load_providers
from omnigent.server.admin_list import resolve_data_dir

_LOCK = threading.Lock()


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
    entry = {
        "kind": draft.kind,
        "display_name": draft.display_name or name,
        "openai": {
            "base_url": draft.base_url,
            "wire_api": draft.wire_api,
            "api_key_ref": reference,
            "models": draft.models,
        },
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


def _row(name: str, entry: dict, admin: bool) -> dict:
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
        "models": list(family.get("models", {}).values()),
        "key_set": key_set,
        "key_source": "env" if reference.startswith("env:") else "stored",
        "editable": True,
    }
    if admin and reference.startswith("env:"):
        row["key_env"] = reference[4:]
    return row


def load_connections(*, admin: bool = False) -> list[dict]:
    from omnigent.runtime import get_caps

    rows = [_row(name, entry, admin) for name, entry in _load().items()]
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
                "models": [llm.model],
                "key_set": bool(connection.get("api_key") or connection.get("profile")),
                "key_source": "config-file",
                "editable": False,
            },
        )
    return rows


def upsert_connection(name: str, draft: ConnectionWrite) -> dict:
    with _LOCK:
        entries = _load()
        entry = _entry(name, draft, entries.get(name))
        if isinstance(draft.key, KeyValue):
            secrets.store_secret(
                f"server-connection:{name}",
                draft.key.value.get_secret_value(),
                home=str(resolve_data_dir()),
            )
        entries[name] = entry
        _write(entries)
        return _row(name, entry, True)


def delete_connection(name: str) -> None:
    validate_name(name)
    with _LOCK:
        entries = _load()
        entries.pop(name, None)
        _write(entries)
        secrets.delete_secret(f"server-connection:{name}", home=str(resolve_data_dir()))


def probe_connection(name: str, draft: ConnectionWrite) -> dict:
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
            ][:200]
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
