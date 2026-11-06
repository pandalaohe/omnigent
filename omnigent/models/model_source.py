"""Host-owned model listings; a listing never changes an agent's connection."""

from __future__ import annotations

import asyncio
import json
import re
import shlex
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlsplit

from omnigent.acp_cli_harnesses import ACP_CLI_HARNESSES
from omnigent.models.model_catalog_store import (
    binary_identity,
    catalog_age_s,
    ensure_catalog,
    fingerprint_of,
    read_catalog,
    reprobe_catalog,
    write_catalog,
)
from omnigent.onboarding.provider_config import load_config


@dataclass(frozen=True)
class ModelSource:
    """Exactly one listing lane from the host owner's global configuration."""

    lane: Literal["url", "command", "static", "agent"]
    url: str | None = None
    api_key_ref: str | None = None
    command: str | None = None
    static: tuple[str, ...] = ()


def model_source_for(harness: str, config: Mapping[str, Any] | None = None) -> ModelSource | None:
    """Read one entry, rejecting malformed declarations without affecting siblings."""
    config = load_config() if config is None else config
    sources = config.get("model_sources", {})
    if not isinstance(sources, Mapping):
        raise ValueError("model_sources must be a mapping")
    if harness not in sources:
        return (
            ModelSource("agent")
            if harness == "acp" or harness.startswith("acp:") or harness in ACP_CLI_HARNESSES
            else None
        )
    raw = sources[harness]
    if not isinstance(raw, Mapping):
        raise ValueError("model source must be a mapping")
    lanes = [key for key in ("url", "command", "static", "agent") if key in raw]
    if len(lanes) != 1 or set(raw) - {"url", "command", "static", "agent", "api_key_ref"}:
        raise ValueError("model source needs exactly one lane and no unknown keys")
    lane = lanes[0]
    ref = raw.get("api_key_ref")
    if ref is not None and (
        lane != "url"
        or not isinstance(ref, str)
        or not re.fullmatch(r"(?:env:[A-Za-z_][A-Za-z0-9_]*|keychain:[A-Za-z0-9_.-]+)", ref)
    ):
        raise ValueError(
            "api_key_ref needs an env:NAME or keychain:name reference on a url source"
        )
    value = raw[lane]
    if lane == "static":
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            raise ValueError("static needs a list of model ids")
        return ModelSource("static", static=tuple(value))
    if lane == "agent":
        if value is not True:
            raise ValueError("agent must be true")
        return ModelSource("agent")
    if not isinstance(value, str) or not value.strip():
        raise ValueError("url and command need non-empty strings")
    if lane == "url":
        try:
            parsed = urlsplit(value)
        except ValueError:
            raise ValueError("url needs a valid HTTP endpoint") from None
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("url needs an HTTP endpoint without credentials, query or fragment")
        return ModelSource("url", url=value, api_key_ref=ref)
    return ModelSource("command", command=value)


def model_source_env_names(config: Mapping[str, Any]) -> frozenset[str]:
    """Only names, including the supported prefixed alias, for daemon forwarding."""
    sources = config.get("model_sources")
    names: set[str] = set()
    for raw in sources.values() if isinstance(sources, Mapping) else ():
        ref = raw.get("api_key_ref") if isinstance(raw, Mapping) else None
        if isinstance(ref, str) and re.fullmatch(r"env:[A-Za-z_][A-Za-z0-9_]*", ref):
            name = ref[4:]
            names.update((name, name if name.startswith("OMNIGENT_") else f"OMNIGENT_{name}"))
    return frozenset(names)


def command_models(output: str) -> list[dict[str, Any]]:
    """Parse a JSON array of ids/rows or id<TAB>label lines."""
    if output.lstrip().startswith("["):
        values = json.loads(output)
        if not isinstance(values, list):
            raise ValueError("command must return a model array")
        rows = [{"id": value} if isinstance(value, str) else value for value in values]
        if any(not isinstance(row, dict) or not isinstance(row.get("id"), str) for row in rows):
            raise ValueError("command rows need string ids")
        return [
            {
                "id": row["id"],
                **(
                    {"displayName": row["displayName"]}
                    if isinstance(row.get("displayName"), str)
                    else {}
                ),
            }
            for row in rows
        ]
    rows = []
    for line in output.splitlines():
        if line.strip():
            model, _, label = line.partition("\t")
            rows.append({"id": model.strip(), "displayName": label.strip() or model.strip()})
    return rows


def _source_catalog(harness: str, source: ModelSource) -> tuple[str, str, Any]:
    if source.lane != "agent":
        return "model-source", fingerprint_of(harness, source), None
    from omnigent.onboarding.acp_auth import AcpAgentEntry, acp_agents, resolve_acp_agent

    entry = (
        resolve_acp_agent(harness[4:])
        if harness.startswith("acp:")
        else next(iter(acp_agents()), None)
        if harness == "acp"
        else None
    )
    if harness in ACP_CLI_HARNESSES:
        from omnigent._platform import resolve_cli_binary
        from omnigent.harness_startup_config import (
            config_harness_path_override,
            resolve_harness_path,
        )

        row = ACP_CLI_HARNESSES[harness]
        executable = (
            resolve_harness_path(harness)
            or config_harness_path_override(harness, load_config())
            or resolve_cli_binary(row.binary)
            or row.binary
        )
        entry = AcpAgentEntry(
            slug=harness, name=row.label, command=shlex.join([executable, *row.args])
        )
    if entry is None:
        raise ValueError("agent source needs a configured ACP agent")
    argv = shlex.split(entry.command)
    if not argv:
        raise ValueError("ACP command is empty")
    return "acp", fingerprint_of(entry.command, binary_identity(argv[0])), entry


def _run_command(command: str) -> list[dict[str, Any]]:
    from omnigent._platform import default_shell_argv
    from omnigent.inner import _proc

    argv = default_shell_argv(command)
    with subprocess.Popen(
        _proc.pipe_supervised_command(*argv),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
        **_proc.spawn_kwargs(),
    ) as process:
        _proc.remember_process_group(process)
        try:
            stdout, _ = process.communicate(timeout=15)
        except BaseException:
            _proc.kill_tree(process)
            process.communicate()
            raise
        if process.returncode:
            raise RuntimeError("model listing command failed")
    return command_models(stdout)


async def fetch_model_source(
    harness: str, source: ModelSource, *, refresh: Literal["none", "auto", "force"] = "none"
) -> list[dict[str, Any]]:
    """Store first; keep a previous nonempty answer on failure or empty refresh."""
    namespace, key, entry = _source_catalog(harness, source)

    failure: str | None = None

    async def resolve() -> list[dict[str, Any]] | None:
        nonlocal failure
        try:
            if source.lane == "static":
                rows = [{"id": value} for value in source.static]
            elif source.lane == "command":
                rows = await asyncio.to_thread(_run_command, source.command or "")
            elif source.lane == "url":
                from omnigent.models.model_catalog import (
                    ResolvedModelProvider,
                    listing_for_provider,
                )
                from omnigent.onboarding.provider_config import resolve_secret

                token = resolve_secret(source.api_key_ref) if source.api_key_ref else None
                listing = await asyncio.to_thread(
                    listing_for_provider,
                    ResolvedModelProvider(
                        kind="local", family="openai", base_url=source.url, api_key=token
                    ),
                    refresh=True,
                )
                if not listing.verified:
                    raise ValueError("model endpoint did not return a verified listing")
                rows = [{"id": model.id} for model in listing.models]
            else:
                from omnigent.inner.acp_harness import probe_acp_model_options

                result = await probe_acp_model_options(entry)
                if result["error"]:
                    raise ValueError("ACP model probe failed")
                rows = result["models"]
                write_catalog("acp-modes", key, result["permission_options"])
            source_label = {
                "url": "URL",
                "command": "Command",
                "static": "Static list",
                "agent": "Agent",
            }[source.lane]
            metadata = {"kind": "model-source", "label": source_label}
            if source.url:
                from omnigent.models.model_catalog import _model_configuration_host

                if host := _model_configuration_host(source.url):
                    metadata["host"] = host
            return (
                [{**row, "source": metadata} for row in rows] or read_catalog(namespace, key) or []
            )
        except Exception as exc:  # noqa: BLE001 — exception text can contain credentials
            # Provider and command exceptions may include headers, argv or stdout.
            failure = f"model source failed ({type(exc).__name__})"
            return None

    age = catalog_age_s(namespace, key)
    if refresh == "force" or (refresh == "auto" and age is not None and age > 300):
        rows = await reprobe_catalog(namespace, key, resolve)
        if rows is None:
            raise RuntimeError(failure or "model source failed")
        return rows
    rows = await ensure_catalog(namespace, key, resolve)
    if rows is None:
        raise RuntimeError(failure or "model source failed")
    return rows
