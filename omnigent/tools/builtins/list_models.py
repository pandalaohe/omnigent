"""Built-in tool: sys_list_models — per-worker model availability.

Registers alongside the sub-agent dispatch surface so an orchestrator
can deterministically learn which models each worker (and it itself)
can run before passing an ``args.model`` to ``sys_session_send``. The
enumeration logic lives in :mod:`omnigent.models.model_catalog`; the runner
dispatches this tool locally (see ``omnigent/runner/tool_dispatch.py``)
and the in-process path runs the same enumerator via :meth:`invoke`.
"""

from __future__ import annotations

import json
from collections.abc import Mapping

# Any: tool schemas are heterogeneous dicts.
from typing import Any

from omnigent.spec.types import AgentSpec
from omnigent.tools.base import Tool, ToolContext

#: Error text for a host-catalog request on a path without server access.
HOST_CATALOG_NEEDS_SERVER = "listing a host catalog requires server access"


def requests_host_catalog(args: Mapping[str, object]) -> bool:
    """
    Whether a call asks for a host's launch catalog instead of worker listings.

    :param args: Parsed tool arguments, e.g. ``{"host": "h1", "agent_id": "ag_1"}``.
    :returns: ``True`` when ``host`` or ``agent_id`` is present and non-null.
    """
    return args.get("host") is not None or args.get("agent_id") is not None


class SysListModelsTool(Tool):
    """
    List the models each sub-agent worker (and the caller) can run.

    Returns a JSON object mapping each declared sub-agent name — plus
    ``"self"`` for the calling agent's own harness — to
    ``{source, verified, models: [{id, family, context_window?}], note}``.
    Lists are resolved from each worker's actual model provider and
    filtered to the model family its harness can run, so every id in a
    worker's list is dispatchable to that worker via
    ``sys_session_send`` ``args.model``.

    :param spec: The calling agent's parsed :class:`AgentSpec`.
    """

    def __init__(self, spec: AgentSpec) -> None:
        """
        Create a list-models tool bound to the calling agent's spec.

        :param spec: The calling agent's spec (sub-agents enumerated
            from ``spec.sub_agents``).
        """
        self._spec = spec

    @classmethod
    def name(cls) -> str:
        """:returns: ``"sys_list_models"``."""
        return "sys_list_models"

    @classmethod
    def description(cls) -> str:
        """:returns: Human-readable description of the tool."""
        return (
            "List the models each sub-agent worker — and this agent "
            "itself, under 'self' — can actually run here, resolved from "
            "each worker's real model provider and filtered to its "
            "harness's model family. Call this BEFORE passing an "
            "unfamiliar 'args.model' to sys_session_send, when the user "
            "asks which models are available, or to preflight a worker "
            "(source 'none' means dispatches to that worker cannot run "
            "in this deployment). Each entry reports {source, verified, "
            "models: [{id, family, context_window?}], note}; pick "
            "'args.model' values verbatim from the target worker's list. "
            "With 'host' and 'agent_id' instead, it lists that host's "
            "cached launch catalog for the agent's harness — the list the "
            "new-session model picker shows — as {host, host_id, agent_id, "
            "harness, source, fetched_at, stale, error, models: [{id, "
            "provider, displayName, isDefault, hidden}]}: filter the rows "
            "by provider and name, then pass a row's 'id' verbatim as the "
            "model for a session of that agent on that host. A native "
            "Antigravity catalog refreshes on read after expiry. 'stale' "
            "indicates a failed sync, an offline host, or an expired native "
            "Antigravity catalog. 'fetched_at' is in Unix seconds."
        )

    def get_schema(self) -> dict[str, Any]:
        """
        Return the OpenAI-format tool schema (both parameters optional).

        :returns: Dict with ``"type": "function"`` and a
            ``"function"`` sub-dict.
        """
        return {
            "type": "function",
            "function": {
                "name": SysListModelsTool.name(),
                "description": SysListModelsTool.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "host": {
                            "type": "string",
                            "description": (
                                "Host id or exact host name whose launch "
                                "catalog to list. Pass together with agent_id."
                            ),
                        },
                        "agent_id": {
                            "type": "string",
                            "description": (
                                "Agent whose harness selects the catalog, "
                                "e.g. an id from sys_agent_list. Pass "
                                "together with host."
                            ),
                        },
                    },
                    "required": [],
                    "additionalProperties": False,
                },
            },
        }

    def invoke(self, arguments: str, ctx: ToolContext) -> str:
        """
        Enumerate per-worker model availability (in-process path).

        This path has no server, so a host-catalog request is refused.

        :param arguments: JSON arguments; ``host`` / ``agent_id`` select
            the host catalog, anything else is ignored.
        :param ctx: Tool execution context (unused).
        :returns: JSON mapping of worker name (plus ``"self"``) to its
            ``{source, verified, models, note}`` row, or a JSON error.
        """
        del ctx
        try:
            parsed = json.loads(arguments) if arguments else {}
        except ValueError:
            parsed = {}
        if isinstance(parsed, dict) and requests_host_catalog(parsed):
            return json.dumps(
                {"error": "server_unavailable", "message": HOST_CATALOG_NEEDS_SERVER}
            )
        from omnigent.models.model_catalog import catalog_for_spec

        return json.dumps(catalog_for_spec(self._spec))
