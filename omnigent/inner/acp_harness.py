"""``harness: acp`` wrap (the generic Agent Client Protocol harness).

Thin module exposing :func:`create_app` — the entry point the shared
:mod:`omnigent.runtime.harnesses._runner` invokes after the parent process
resolves ``"acp"`` (or ``"acp:<slug>"``) to this module.

Wraps an :class:`omnigent.inner.acp_executor.AcpExecutor`, which drives *any*
ACP agent command over the Agent Client Protocol — the vendor-agnostic
counterpart to the ``goose`` / ``qwen`` wraps. Which agent runs is decided by
the spawn-env the runner passes (see
:func:`omnigent.runtime.workflow._build_acp_spawn_env`), which resolves the
picked ``acp:<slug>`` to a user-configured command in the ``acp:`` config block.

Auth is each agent's own (the user logs into their agent via its own CLI);
Omnigent stores no credential. Tool approvals surface as web elicitation cards
via ``session/request_permission`` (bridges the :class:`ExecutorAdapter` installs).

Env vars read at startup:

- ``HARNESS_ACP_COMMAND`` (required): the command to launch, e.g.
  ``"gemini --experimental-acp"``. Missing → a request-time error.
- ``HARNESS_ACP_NAME``: display label for logs / elicitation cards.
- ``HARNESS_ACP_MODEL``: optional model id (only sent when the agent is
  configured to accept one in ``session/new``).
- ``HARNESS_ACP_DEFAULT_MODEL``: model restored when an override is cleared.
  Empty uses the ACP session's initial model; unset uses the launch model.
- ``HARNESS_ACP_MODEL_LIST``: comma-separated curated model ids from the agent's
  explicitly bound provider. When set, warm model switches to ids outside
  the list are withheld; unset means any model the agent accepts.
- ``HARNESS_ACP_ENV_UNSET``: comma-separated environment variable *names* to
  strip from the spawn env handed to the vendor CLI (operator-declared, e.g.
  dummy tokens for other providers that would activate the CLI's built-ins).
  Names only — unset means no scrubbing.
- ``HARNESS_ACP_SESSION_ID_MODE``: ``server`` (default) or ``client``.
- ``HARNESS_ACP_SEND_MODEL``: ``"1"`` to send the model in ``session/new``.
- ``HARNESS_ACP_OMNIGENT_MCP``: ``"0"`` to disable Omnigent's MCP relay;
  ``session/new`` still receives an empty ``mcpServers`` array.
- ``HARNESS_ACP_OS_ENV``: JSON-encoded :class:`OSEnvSpec`. When unset, falls
  back to ``caller_process`` + ``sandbox=none``.
- ``HARNESS_ACP_ENV_PASSTHROUGH``: comma-separated environment variable *names*
  the agent may read at spawn (the spawn env is otherwise deny-by-default, so an
  agent authenticating from a variable needs it named here). Names only — each
  value is read from this process's own environment.
- ``HARNESS_ACP_PROMPT_TIMEOUT_S``: optional idle (time-without-progress) deadline in
  seconds for a prompt turn (default 300); must be positive and finite or the child aborts.
- ``HARNESS_ACP_INIT_TIMEOUT_S``: positive finite seconds per handshake/session-setup
  RPC (default 30), forwarded from the agent's ``init_timeout_s`` configuration.
- ``HARNESS_ACP_PERMISSION_MODE``: Omnigent permission stance, ``auto`` (default) or
  ``bypassPermissions`` — the latter skips the approval card for a tool call no
  policy had an opinion on, so a headless agent runs without parking on prompts.
- ``HARNESS_ACP_INJECT_SYSTEM_PROMPT``: ``"0"`` to skip folding the Omnigent system
  prompt into the first ACP turn. Recommended for Pi-fork agents (e.g. ``omp``) that
  fully own their own system prompt — prepending Omnigent's text can cause the agent's
  internal Claude model to emit XML tool-call fragments when no MCP relay is backing the
  described tools (see ``omnigent_mcp``). Defaults to ``"1"`` (inject).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from typing import Any, TypedDict

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from omnigent.cli_invocation import cli_invocation
from omnigent.inner import _proc
from omnigent.inner.acp_executor import AcpAgentConfig, AcpExecutor
from omnigent.inner.acp_extension import NO_ACP_EXTENSION, AcpExtension
from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
from omnigent.inner.executor import Executor
from omnigent.inner.os_env_serialization import decode_sandbox_spec
from omnigent.onboarding.acp_auth import AcpAgentEntry, parse_init_timeout_s
from omnigent.runtime.harnesses._executor_adapter import ExecutorAdapter

_logger = logging.getLogger(__name__)

_ENV_COMMAND = "HARNESS_ACP_COMMAND"
_ENV_NAME = "HARNESS_ACP_NAME"
_ENV_MODEL = "HARNESS_ACP_MODEL"
_ENV_DEFAULT_MODEL = "HARNESS_ACP_DEFAULT_MODEL"
_ENV_MODEL_LIST = "HARNESS_ACP_MODEL_LIST"
_ENV_ENV_UNSET = "HARNESS_ACP_ENV_UNSET"
_ENV_SESSION_ID_MODE = "HARNESS_ACP_SESSION_ID_MODE"
_ENV_SEND_MODEL = "HARNESS_ACP_SEND_MODEL"
_ENV_OMNIGENT_MCP = "HARNESS_ACP_OMNIGENT_MCP"
_ENV_INJECT_SYSTEM_PROMPT = "HARNESS_ACP_INJECT_SYSTEM_PROMPT"
_ENV_CWD = "HARNESS_ACP_CWD"
_ENV_OS_ENV = "HARNESS_ACP_OS_ENV"
_ENV_ENV_PASSTHROUGH = "HARNESS_ACP_ENV_PASSTHROUGH"
_ENV_PERMISSION_MODE = "HARNESS_ACP_PERMISSION_MODE"
_DEFAULT_PERMISSION_MODE = "auto"


class AcpProbeResult(TypedDict):
    models: list[dict[str, Any]]
    permission_options: list[dict[str, str]]
    error: str | None


class _CatalogProbeExecutor(AcpExecutor):
    async def _read_stderr(self) -> None:
        # A listing command's diagnostics can contain credentials.
        assert self._proc and self._proc.stderr
        while await self._proc.stderr.read(65536):
            pass

    async def _read_stdout_unobserved(self) -> None:
        assert self._proc and self._proc.stdout
        try:
            while line := await self._proc.stdout.readline():
                try:
                    message = json.loads(line)
                except (ValueError, UnicodeError):
                    continue
                if not isinstance(message, dict):
                    continue
                identifier = message.get("id")
                if not isinstance(identifier, int | str):
                    continue
                if "method" in message:
                    await self._send(
                        {
                            "jsonrpc": "2.0",
                            "id": identifier,
                            "error": {
                                "code": -32601,
                                "message": "Model probes do not execute agent requests",
                            },
                        }
                    )
                else:
                    future = (
                        self._pending.pop(identifier, None)
                        if isinstance(identifier, int)
                        else None
                    )
                    if future is not None and not future.done():
                        future.set_result(message)
        finally:
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(EOFError("ACP model probe stopped"))
            self._pending.clear()


async def probe_acp_model_options(
    entry: AcpAgentEntry, *, timeout_s: float = 30.0
) -> AcpProbeResult:
    """Ask only initialize/session-new in a disposable cwd, then stop the process."""
    models: list[dict[str, Any]] = []
    permissions: list[dict[str, str]] = []
    executor: AcpExecutor | None = None
    try:
        timeout_s = min(parse_init_timeout_s(timeout_s), 30.0)
        cleanup_budget = min(5.0, timeout_s / 5)
        with tempfile.TemporaryDirectory(prefix="model-probe-") as cwd:
            executor = _CatalogProbeExecutor(
                config=AcpAgentConfig(
                    command=entry.command,
                    name="model probe",
                    env_passthrough=entry.env_passthrough,
                    env_unset=_csv_names("OMNIGENT_ACP_ENV_UNSET"),
                    omnigent_mcp=False,
                ),
                cwd=cwd,
            )
            try:
                async with asyncio.timeout(max(0.001, timeout_s - cleanup_budget)):
                    await executor._start_process()
                    if executor._proc is not None:
                        _proc.remember_process_group(executor._proc)
                    initialized = await executor._rpc(
                        "initialize",
                        {
                            "protocolVersion": 1,
                            "clientCapabilities": {
                                "fs": {"readTextFile": False, "writeTextFile": False},
                                "terminal": False,
                            },
                        },
                    )
                    if "error" in initialized:
                        raise RuntimeError("initialize refused")
                    params: dict[str, Any] = {"cwd": cwd, "mcpServers": []}
                    if entry.session_id_mode == "client":
                        params["sessionId"] = "model-probe"
                    if entry.send_model and entry.model:
                        params["model"] = entry.model
                    response = await executor._rpc("session/new", params)
                    if "error" in response:
                        raise RuntimeError("session/new refused or authentication required")
                    result = response.get("result") or {}
                    legacy = result.get("models") or {}
                    current = legacy.get("currentModelId")
                    models = [
                        {
                            "id": item["modelId"],
                            "displayName": item.get("name", item["modelId"]),
                            "isActive": item["modelId"] == current,
                            "modelSwitch": True,
                        }
                        for item in legacy.get("availableModels", [])
                        if isinstance(item, dict) and isinstance(item.get("modelId"), str)
                    ]
                    for option in result.get("configOptions") or []:
                        if not isinstance(option, dict):
                            continue
                        choices = AcpExecutor._select_choices(option.get("options"))
                        if option.get("category", option.get("id")) == "model":
                            models = [
                                {
                                    "id": choice["value"],
                                    "displayName": choice.get("name", choice["value"]),
                                    "isActive": choice["value"] == option.get("currentValue"),
                                    "modelSwitch": True,
                                }
                                for choice in choices
                            ]
                        if option.get("category", option.get("id")) == "mode":
                            permissions = [
                                {
                                    "value": choice["value"],
                                    "label": str(choice.get("name") or choice["value"]),
                                }
                                for choice in choices
                            ]
                    effort_option = next(
                        (
                            option
                            for option in result.get("configOptions") or []
                            if isinstance(option, dict)
                            and option.get("category") == "thought_level"
                        ),
                        {},
                    )
                    for row in models:
                        row["isDefault"] = row.get("isActive", False)
                        if row.get("isActive") and effort_option:
                            row["efforts"] = [
                                choice["value"]
                                for choice in AcpExecutor._select_choices(
                                    effort_option.get("options")
                                )
                            ]
                    if not permissions:
                        modes = result.get("modes") or {}
                        permissions = [
                            {"value": item["id"], "label": item.get("name", item["id"])}
                            for item in modes.get("availableModes", [])
                            if isinstance(item, dict) and isinstance(item.get("id"), str)
                        ]
                    start = (
                        initialized.get("result", {})
                        .get("_meta", {})
                        .get("omnigent", {})
                        .get("start_efforts")
                    )
                    if isinstance(start, list):
                        for row in models:
                            row["start_efforts"] = [
                                value for value in start if isinstance(value, str)
                            ]
                    caps = (
                        initialized.get("result", {})
                        .get("agentCapabilities", {})
                        .get("sessionCapabilities", {})
                    )
                    # Capture helpers before session/close can make their parent exit.
                    _proc.refresh_process_tree(executor._proc)
                    if (
                        caps.get("close") is not None
                        and caps.get("close") is not False
                        and result.get("sessionId")
                    ):
                        await executor._rpc("session/close", {"sessionId": result["sessionId"]})
            finally:
                process = executor._proc
                _proc.refresh_process_tree(process)
                try:
                    await asyncio.wait_for(executor.close(), timeout=cleanup_budget * 0.8)
                finally:
                    if process is not None:
                        _proc.kill_tree(process)
                        await asyncio.wait_for(process.wait(), timeout=cleanup_budget * 0.2)
    except Exception as exc:  # noqa: BLE001 — never expose agent output in a catalog error
        return {
            "models": [],
            "permission_options": [],
            "error": f"ACP probe failed ({type(exc).__name__})",
        }
    return {"models": models, "permission_options": permissions, "error": None}


def _env_enabled(name: str, *, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_passthrough_names() -> tuple[str, ...]:
    """Variable names the configured agent may read, from the spawn env.

    Comma-separated names (never values — the parent forwards only names, and
    the value is read from this process's own environment at spawn).
    """
    raw = os.environ.get(_ENV_ENV_PASSTHROUGH, "")
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def _csv_names(env_name: str) -> tuple[str, ...]:
    """Parse a comma-separated, deduplicated name list from the env."""
    raw = os.environ.get(env_name, "")
    names: list[str] = []
    for part in raw.split(","):
        name = part.strip()
        if name and name not in names:
            names.append(name)
    return tuple(names)


def _resolve_os_env() -> OSEnvSpec:
    """Resolve the inner-executor :class:`OSEnvSpec` from env config.

    Decodes the JSON-encoded :data:`_ENV_OS_ENV`; falls back to
    ``caller_process`` + ``sandbox=none`` when the var is missing or malformed.
    """
    raw = os.environ.get(_ENV_OS_ENV, "").strip()
    if raw:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            _logger.warning(
                "%s is not valid JSON (%s); falling back to default os_env", _ENV_OS_ENV, exc
            )
            payload = None
        if isinstance(payload, dict):
            sandbox_payload = payload.get("sandbox")
            sandbox = (
                decode_sandbox_spec(sandbox_payload) if isinstance(sandbox_payload, dict) else None
            )
            return OSEnvSpec(
                type=str(payload.get("type", "caller_process")),
                cwd=payload.get("cwd"),
                sandbox=sandbox,
                fork=bool(payload.get("fork", False)),
            )
    return OSEnvSpec(
        type="caller_process",
        cwd=None,
        sandbox=OSEnvSandboxSpec(type="none"),
        fork=False,
    )


def _build_acp_executor(extension: AcpExtension = NO_ACP_EXTENSION) -> AcpExecutor:
    """Construct an :class:`AcpExecutor` from env-var config (lazily, on first turn).

    :param extension: Vendor behavior to inject, from the calling wrap. Defaults
        to protocol-only for the generic ``acp`` harness.
    """
    command = os.environ.get(_ENV_COMMAND, "").strip()
    if not command:
        raise RuntimeError(
            f"{_ENV_COMMAND} is not set — no ACP agent command configured. "
            f"Add one via `{cli_invocation()} setup` → configure harnesses → Custom ACP agent."
        )
    name = os.environ.get(_ENV_NAME, "").strip() or "ACP agent"
    model = os.environ.get(_ENV_MODEL, "").strip() or None
    session_id_mode = os.environ.get(_ENV_SESSION_ID_MODE, "").strip() or "server"
    send_model = _env_enabled(_ENV_SEND_MODEL, default=False)
    omnigent_mcp = _env_enabled(_ENV_OMNIGENT_MCP, default=True)
    inject_system_prompt = _env_enabled(_ENV_INJECT_SYSTEM_PROMPT, default=True)
    cwd = os.environ.get(_ENV_CWD) or os.environ.get("OMNIGENT_RUNNER_WORKSPACE") or None
    permission_mode = os.environ.get(_ENV_PERMISSION_MODE, "").strip() or _DEFAULT_PERMISSION_MODE

    config = AcpAgentConfig(
        command=command,
        name=name,
        model=model,
        default_model=os.environ.get(_ENV_DEFAULT_MODEL),
        session_id_mode=session_id_mode,
        send_model_in_session_new=send_model,
        omnigent_mcp=omnigent_mcp,
        env_passthrough=_env_passthrough_names(),
        available_models=_csv_names(_ENV_MODEL_LIST),
        env_unset=_csv_names(_ENV_ENV_UNSET),
        permission_mode=permission_mode,
        inject_system_prompt=inject_system_prompt,
        init_timeout_s=parse_init_timeout_s(
            float(os.environ.get("HARNESS_ACP_INIT_TIMEOUT_S", "30"))
        ),
    )
    return AcpExecutor(config=config, cwd=cwd, os_env=_resolve_os_env(), extension=extension)


def create_app(extension: AcpExtension = NO_ACP_EXTENSION) -> FastAPI:
    """Build the generic ACP harness's FastAPI app (required entry point).

    The wrapped :class:`AcpExecutor` is constructed lazily on the first turn, so
    a missing command / absent agent binary surfaces as a request-time error
    rather than an app-boot crash.

    :param extension: Vendor behavior for the agent this process drives. A
        vendor's own wrap calls this with its extension (see
        a vendor wrap); the runner calls it with no
        argument for ``harness: acp`` and for a builtin ACP CLI row that declares
        no vendor behavior.
    :returns: The app the runner serves.
    """
    label = os.environ.get(_ENV_NAME, "").strip() or "ACP agent"
    executor: AcpExecutor | None = None

    def factory() -> Executor:
        nonlocal executor
        executor = _build_acp_executor(extension)
        return executor

    adapter = ExecutorAdapter(executor_factory=factory, harness_label=label, startup_source="acp")
    app = adapter.build()

    @app.get("/v1/model-options")
    async def model_options(request: Request) -> Response:
        if (denied := adapter._check_auth(request)) is not None:
            return denied
        if executor is None or not executor.model_options_ready:
            return JSONResponse({"models": []}, status_code=503)
        return JSONResponse({"models": executor.model_options()})

    return app
