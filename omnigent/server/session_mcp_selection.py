"""How a deployment records the MCP services a session starts with.

Interactive sessions pick MCP services when they are created. Sessions the
server creates on someone's behalf, a scheduled task's fired sessions and the
workers a session starts, must start with the same services, so the server
needs to turn a stored list of service names into session labels and read it
back. The registry default writes ``omnigent.mcp_service.<id>`` labels, which
the runner applies when it loads the agent spec. A deployment that configures
MCP servers some other way (for example, inside a sandbox at launch) passes its
own :class:`SessionMcpSelection` to ``create_app``.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Literal, Protocol

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.mcp_selection import MCP_SELECTION_PREFIX, registry_selection_labels
from omnigent.server.auth import RESERVED_USER_LOCAL
from omnigent.server.routes.connections_base import ConnectionError
from omnigent.server.runtime_compatibility import require_runtime_support
from omnigent.server.schemas import MAX_MCP_REGISTRY_SERVICES, MCP_REGISTRY_SERVICE_ID_PATTERN

if TYPE_CHECKING:
    from collections.abc import Collection

    from fastapi import Request

    from omnigent.entities.agent import Agent
    from omnigent.runner.routing import RunnerRouter
    from omnigent.runner.transports.ws_tunnel.registry import TunnelRegistry
    from omnigent.runtime.agent_cache import AgentCache
    from omnigent.server.host_registry import HostRegistry
    from omnigent.server.mcp_registry import McpRegistry
    from omnigent.spec import AgentSpec

_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ResolvedSessionMcpSelection:
    source: Literal["explicit", "default", "copied"]
    labels: dict[str, str]

    def for_runtime(
        self,
        *,
        host_id: str | None,
        runner_id: str | None = None,
        host_registry: HostRegistry | None = None,
        tunnel_registry: TunnelRegistry | None = None,
        authored_services: Collection[str] = (),
        tcp_egress_required: bool = False,
        runner_router: RunnerRouter | None = None,
    ) -> ResolvedSessionMcpSelection:
        """Keep existing capability errors for authored and explicit services."""
        services = {
            key.removeprefix(MCP_SELECTION_PREFIX)
            for key, value in self.labels.items()
            if value == "1"
        }

        def require(selected: Collection[str], *, requires_tcp: bool = False) -> None:
            require_runtime_support(
                selected,
                tcp_egress_required=requires_tcp,
                host_id=host_id,
                runner_id=runner_id,
                host_registry=host_registry,
                tunnel_registry=tunnel_registry,
                runner_router=runner_router,
            )

        if self.source != "default":
            require(set(authored_services) | services, requires_tcp=tcp_egress_required)
            return self
        require(authored_services, requires_tcp=tcp_egress_required)
        try:
            require(services)
        except OmnigentError as exc:
            if exc.code != ErrorCode.RUNNER_CAPABILITY_MISMATCH:
                raise
            _logger.info("Skipping default MCP services for an incompatible runtime: %s", exc)
            return replace(self, labels={})
        return self


def require_registry_service(
    registry: McpRegistry | None, service_id: str, user_id: str | None
) -> None:
    try:
        if registry is None:
            raise ConnectionError("MCP registry is not configured")
        registry.service(service_id, user_id or RESERVED_USER_LOCAL)
    except ConnectionError as exc:
        raise OmnigentError(str(exc), code=ErrorCode.FORBIDDEN) from exc


def resolve_session_mcp_selection(
    registry: McpRegistry | None,
    spec: AgentSpec | None,
    user_id: str | None,
    *,
    explicit_services: list[str] | None = None,
    source_labels: Mapping[str, str] | None = None,
    named_subagent: bool = False,
) -> ResolvedSessionMcpSelection:
    if source_labels is not None:
        return ResolvedSessionMcpSelection(
            "copied",
            {
                key: value
                for key, value in source_labels.items()
                if key.startswith(MCP_SELECTION_PREFIX)
            },
        )
    if explicit_services is not None:
        if not explicit_services:
            return ResolvedSessionMcpSelection("explicit", {})
        if named_subagent:
            raise OmnigentError(
                "Named sub-agents use their authored MCP services", code=ErrorCode.INVALID_INPUT
            )
        if spec is None:
            raise OmnigentError(
                "Cannot load the agent's MCP configuration", code=ErrorCode.INVALID_INPUT
            )
        for service_id in explicit_services:
            require_registry_service(registry, service_id, user_id)
        return ResolvedSessionMcpSelection(
            "explicit", registry_selection_labels(spec, explicit_services)
        )

    if registry is None or spec is None or named_subagent:
        return ResolvedSessionMcpSelection("default", {})
    authored = {server.name for server in spec.mcp_servers}
    defaults = []
    for service_id in registry.config.default_services:
        if service_id in authored:
            continue
        try:
            registry.service(service_id, user_id or RESERVED_USER_LOCAL)
        except ConnectionError:
            continue
        defaults.append(service_id)
    return ResolvedSessionMcpSelection("default", registry_selection_labels(spec, defaults))


async def select_default_mcp_for_agent(
    registry: McpRegistry | None,
    agent: Agent | None,
    agent_cache: AgentCache | None,
    user_id: str | None,
) -> ResolvedSessionMcpSelection:
    """Load a stored bundle only when there are defaults that could apply."""
    if registry is None or not registry.config.default_services or agent is None:
        return ResolvedSessionMcpSelection("default", {})
    if agent_cache is None:
        from omnigent.runtime import get_agent_cache

        agent_cache = get_agent_cache()
    try:
        bundle = await asyncio.to_thread(
            agent_cache.load, agent.id, agent.bundle_location, expand_env=agent.operator_authored
        )
    except (KeyError, AttributeError, ValueError, ImportError, OSError):
        _logger.debug("Could not load agent %s for default MCP selection", agent.id, exc_info=True)
        return ResolvedSessionMcpSelection("default", {})
    return resolve_session_mcp_selection(registry, bundle.spec, user_id)


_REGISTRY_SERVICE_ID = re.compile(MCP_REGISTRY_SERVICE_ID_PATTERN)
MAX_SESSION_MCP_SERVICES = MAX_MCP_REGISTRY_SERVICES


class SessionMcpSelection(Protocol):
    """Validates, writes and reads a session's MCP service selection."""

    def validate(
        self,
        request: Request,
        services: Sequence[str],
        *,
        spec: AgentSpec | None,
        user_id: str | None,
    ) -> tuple[str, ...]:
        """Return the de-duplicated services, or raise ``OmnigentError``.

        Called when an owner saves the selection, so it may require the owner
        to be signed in to every service.
        """
        ...

    def session_labels(self, services: Sequence[str]) -> dict[str, str]:
        """Labels that make a new session start with *services*."""
        ...

    def selected_services(self, labels: Mapping[str, str]) -> tuple[str, ...]:
        """The services a session's *labels* select, in label order."""
        ...


class RegistryMcpSelection:
    """Selects services from the server's MCP registry."""

    def validate(
        self,
        request: Request,
        services: Sequence[str],
        *,
        spec: AgentSpec | None,
        user_id: str | None,
    ) -> tuple[str, ...]:
        from omnigent.server.auth import RESERVED_USER_LOCAL
        from omnigent.server.routes.session_mcp_servers import _require_registry_service

        unique = _unique_services(services)
        owner = user_id or RESERVED_USER_LOCAL
        for service in unique:
            if not _REGISTRY_SERVICE_ID.match(service):
                raise OmnigentError(
                    f"Invalid MCP service id {service!r}", code=ErrorCode.INVALID_INPUT
                )
            _require_registry_service(request, service, user_id)
            registry = request.app.state.mcp_registry
            if not registry.connected(registry.services[service], owner, request.app.state):
                raise OmnigentError(
                    f"Connect your {service} account before selecting it",
                    code=ErrorCode.FORBIDDEN,
                )
        if spec is not None:
            # Rejects a service whose name the agent already uses for a custom server.
            registry_selection_labels(spec, list(unique))
        return unique

    def session_labels(self, services: Sequence[str]) -> dict[str, str]:
        return {MCP_SELECTION_PREFIX + service: "1" for service in services}

    def selected_services(self, labels: Mapping[str, str]) -> tuple[str, ...]:
        return tuple(
            key.removeprefix(MCP_SELECTION_PREFIX)
            for key, value in labels.items()
            if key.startswith(MCP_SELECTION_PREFIX) and value == "1"
        )


def session_mcp_selection(app_state: Any) -> SessionMcpSelection:
    """The deployment's selection, or the registry default when none is configured."""
    configured = getattr(app_state, "session_mcp_selection", None) if app_state else None
    return configured if configured is not None else RegistryMcpSelection()


def _unique_services(services: Sequence[str]) -> tuple[str, ...]:
    unique = tuple(dict.fromkeys(services))
    if len(unique) > MAX_SESSION_MCP_SERVICES:
        raise OmnigentError(
            f"Select at most {MAX_SESSION_MCP_SERVICES} MCP services",
            code=ErrorCode.INVALID_INPUT,
        )
    return unique
