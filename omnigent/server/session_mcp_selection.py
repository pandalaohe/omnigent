"""Resolve the registry selection before a new session can launch."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Literal

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.mcp_selection import MCP_SELECTION_PREFIX, registry_selection_labels
from omnigent.server.auth import RESERVED_USER_LOCAL
from omnigent.server.mcp_compatibility import require_registry_mcp_runtime
from omnigent.server.routes.connections_base import ConnectionError

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping

    from omnigent.entities.agent import Agent
    from omnigent.runner.transports.ws_tunnel.registry import TunnelRegistry
    from omnigent.runtime.agent_cache import AgentCache
    from omnigent.server.host_registry import HostRegistry
    from omnigent.server.mcp_registry import McpRegistry
    from omnigent.spec import AgentSpec

_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SessionMcpSelection:
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
    ) -> SessionMcpSelection:
        """Keep existing capability errors for authored and explicit services."""
        services = {
            key.removeprefix(MCP_SELECTION_PREFIX)
            for key, value in self.labels.items()
            if value == "1"
        }

        def require(selected: Collection[str]) -> None:
            require_registry_mcp_runtime(
                selected,
                host_id=host_id,
                runner_id=runner_id,
                host_registry=host_registry,
                tunnel_registry=tunnel_registry,
            )

        if self.source != "default":
            require(set(authored_services) | services)
            return self
        require(authored_services)
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
) -> SessionMcpSelection:
    if source_labels is not None:
        return SessionMcpSelection(
            "copied",
            {
                key: value
                for key, value in source_labels.items()
                if key.startswith(MCP_SELECTION_PREFIX)
            },
        )
    if explicit_services is not None:
        if not explicit_services:
            return SessionMcpSelection("explicit", {})
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
        return SessionMcpSelection("explicit", registry_selection_labels(spec, explicit_services))

    if registry is None or spec is None or named_subagent:
        return SessionMcpSelection("default", {})
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
    return SessionMcpSelection("default", registry_selection_labels(spec, defaults))


async def select_default_mcp_for_agent(
    registry: McpRegistry | None,
    agent: Agent | None,
    agent_cache: AgentCache | None,
    user_id: str | None,
) -> SessionMcpSelection:
    """Load a stored bundle only when there are defaults that could apply."""
    if registry is None or not registry.config.default_services or agent is None:
        return SessionMcpSelection("default", {})
    if agent_cache is None:
        from omnigent.runtime import get_agent_cache

        agent_cache = get_agent_cache()
    try:
        bundle = await asyncio.to_thread(
            agent_cache.load, agent.id, agent.bundle_location, expand_env=agent.operator_authored
        )
    except (KeyError, AttributeError, ValueError, ImportError, OSError):
        _logger.debug("Could not load agent %s for default MCP selection", agent.id, exc_info=True)
        return SessionMcpSelection("default", {})
    return resolve_session_mcp_selection(registry, bundle.spec, user_id)
