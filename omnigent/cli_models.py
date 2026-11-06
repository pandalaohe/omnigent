"""Offline checks for model listings, without starting a server or host daemon."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
from importlib.resources import files
from pathlib import Path
from typing import Any

import click
import yaml

from omnigent.models.model_source import fetch_model_source, model_source_for


@click.group()
def models() -> None:
    """Check model lists and print setup rules."""


@models.command()
def guide() -> None:
    """Print the packaged model connection rules and examples."""
    click.echo(files("omnigent.resources").joinpath("connect_models.md").read_text())


async def _pick_status(
    harness: str, rows: list[dict[str, Any]], config: dict[str, Any], *, agent_source: bool
) -> tuple[str, str]:
    from omnigent.models.model_override import (
        harness_supports_model_override,
        model_family_mismatch,
    )

    if harness == "acp" or harness.startswith("acp:"):
        from omnigent.onboarding.acp_auth import acp_agents, resolve_acp_agent

        entry = (
            resolve_acp_agent(harness[4:], config)
            if harness.startswith("acp:")
            else next(iter(acp_agents(config)), None)
        )
        if entry is None:
            return "needs-binding", "Configure this agent under acp.agents."
        advertised = rows
        if not entry.send_model and not agent_source:
            from omnigent.inner.acp_harness import probe_acp_model_options

            probe = await probe_acp_model_options(entry)
            if probe["error"]:
                return "unknown", "The agent could not be probed; check its CLI login."
            advertised = probe["models"]
        if entry.send_model or any(row.get("modelSwitch") for row in advertised):
            return "reaches", "The agent advertises model selection or accepts a launch model."
        return (
            "list-only",
            "The agent did not advertise model selection. "
            "Configure a launch model in its command.",
        )
    if not harness_supports_model_override(harness):
        return "list-only", "This harness has no model override transport."
    if any(model_family_mismatch(harness, row["id"]) for row in rows):
        return "refused", "Some listed models do not match this harness's supported model family."
    from omnigent.models.model_catalog import resolve_model_provider
    from omnigent.spec.types import AgentSpec, ExecutorSpec

    spec = AgentSpec(
        spec_version=1,
        name="models-check",
        executor=ExecutorSpec(type="omnigent", config={"harness": harness}),
    )
    if resolve_model_provider(spec, harness).kind == "none":
        return (
            "needs-binding",
            "Configure the agent's provider or CLI login before choosing a model.",
        )
    return "reaches", "The harness accepts model overrides; its connection must serve these ids."


async def check_models(harness: str | None) -> tuple[dict[str, Any], int]:
    from omnigent.onboarding.provider_config import _config_path

    result: dict[str, Any] = {"schema_version": 1, "config_errors": [], "harnesses": []}
    try:
        path = Path(_config_path())
        config = yaml.safe_load(path.read_text()) if path.exists() else {}
        if config is None:
            config = {}
        if not isinstance(config, dict):
            raise ValueError("global config must be a mapping")
    except (OSError, ValueError, yaml.YAMLError) as exc:
        result["config_errors"].append(f"global config could not be read ({type(exc).__name__})")
        return result, 2
    sources = config.get("model_sources", {})
    if not isinstance(sources, dict):
        result["config_errors"].append("model_sources must be a mapping")
        return result, 2
    if any(not isinstance(name, str) or not name.strip() for name in sources):
        result["config_errors"].append("model_sources keys must be harness ids")
        return result, 2
    from omnigent.harness_plugins import harness_capabilities
    from omnigent.onboarding.acp_auth import acp_agents

    try:
        configured_acp = [f"acp:{entry.slug}" for entry in acp_agents(config)]
    except ValueError:
        result["config_errors"].append("acp.agents contains an invalid setting")
        return result, 2
    known = set(harness_capabilities()) | set(configured_acp) | set(sources)
    if harness and harness not in known:
        result["config_errors"].append("unknown harness")
        return result, 2
    targets = (
        [harness]
        if harness
        else sorted((set(harness_capabilities()) - {"acp"}) | set(sources) | set(configured_acp))
    )
    code = 0
    for target in targets:
        row: dict[str, Any] = {
            "harness": target,
            "lane": "builtin",
            "status": "empty",
            "count": 0,
            "sample": [],
            "invalid_ids": [],
            "pick": "unknown",
            "error": None,
            "hint": "",
        }
        try:
            source = model_source_for(target, config)
        except ValueError as exc:
            result["config_errors"].append(f"{target}: {exc}")
            code = 2
            continue
        try:
            if source is not None:
                row["lane"] = source.lane
                if (
                    harness is None
                    and source.lane == "agent"
                    and target not in sources
                    and target not in configured_acp
                ):
                    from omnigent.models.model_source import _source_catalog
                    from omnigent.onboarding.acp_auth import command_binary_on_path

                    _, _, entry = _source_catalog(target, source)
                    if not command_binary_on_path(entry.command):
                        row.update(
                            status="skipped", hint="The agent CLI is not installed on this host."
                        )
                        result["harnesses"].append(row)
                        continue
                rows = await fetch_model_source(target, source, refresh="force")
            else:
                from omnigent.models.model_catalog import list_models_for_worker
                from omnigent.spec.types import AgentSpec, ExecutorSpec

                spec = AgentSpec(
                    spec_version=1,
                    name="models-check",
                    executor=ExecutorSpec(type="omnigent", config={"harness": target}),
                )
                listing = await asyncio.to_thread(list_models_for_worker, spec, target)
                rows = [{"id": model.id} for model in listing.models]
                if not listing.verified and not rows:
                    row["hint"] = (
                        "No verified built-in model list. Configure the agent or a model source."
                    )
            from omnigent.models.model_override import validate_model_override

            valid = []
            for item in rows:
                try:
                    validate_model_override(item["id"])
                    valid.append(item)
                except ValueError:
                    # Invalid text can be command diagnostics or a credential.
                    row["invalid_ids"].append("invalid model id")
            row.update(
                count=len(rows),
                sample=[item["id"] for item in valid[:3]],
                status="ok" if rows else "empty",
            )
            row["pick"], row["hint"] = await _pick_status(
                target, valid, config, agent_source=bool(source and source.lane == "agent")
            )
            inference = config.get("inference") or {}
            bindings = inference.get("harnesses") or {} if isinstance(inference, dict) else {}
            binding = bindings.get(target) or {} if isinstance(bindings, dict) else {}
            allowed = binding.get("model_allowlist") if isinstance(binding, dict) else None
            if isinstance(allowed, list) and any(item["id"] not in allowed for item in valid):
                row.update(
                    pick="refused", hint="The listing conflicts with inference model_allowlist."
                )
            if source and source.api_key_ref and source.api_key_ref.startswith("env:"):
                name = source.api_key_ref[4:]
                if not (os.environ.get(name) or os.environ.get(f"OMNIGENT_{name}")):
                    row["hint"] = f"{name} is absent. Export it and restart the host daemon."
                else:
                    row["hint"] += f" Restart the host daemon after adding env:{name}."
            if row["invalid_ids"]:
                row.update(status="failed", error="listing contains invalid model ids")
                code = max(code, 1)
        except Exception as exc:  # noqa: BLE001 — never render provider/command exception text
            row.update(status="failed", error=f"source failed ({type(exc).__name__})")
            if source and source.api_key_ref and source.api_key_ref.startswith("env:"):
                row["hint"] = (
                    f"Check {source.api_key_ref[4:]} in the host daemon environment; "
                    "restart after adding it."
                )
            code = max(code, 1)
        result["harnesses"].append(row)
    return result, code


@models.command("check")
@click.argument("harness", required=False)
@click.option("--json", "as_json", is_flag=True, help="Print the machine-readable check result.")
def check(harness: str | None, as_json: bool) -> None:
    """Check configured sources, or one harness, on this host."""
    with contextlib.redirect_stdout(sys.stderr):
        result, code = asyncio.run(check_models(harness))
    if as_json:
        click.echo(json.dumps(result, ensure_ascii=True))
    else:
        for error in result["config_errors"]:
            click.echo(error)
        for row in result["harnesses"]:
            click.echo(
                f"{row['harness']}: {row['status']}, {row['count']} models, pick: {row['pick']}"
            )
            if row["error"] or row["hint"]:
                click.echo(row["error"] or row["hint"])
    # The console entry uses standalone_mode=False, which drops Click's exit return.
    raise SystemExit(code)
