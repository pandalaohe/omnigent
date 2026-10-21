"""Pure composition of route layers and exclusive session-role instructions."""

import logging
from collections.abc import Sequence
from dataclasses import dataclass

from omnigent.harness_aliases import canonicalize_harness
from omnigent.harness_capabilities import InstructionDelivery
from omnigent.harness_plugins import (
    capability_for_harness,
    harness_labels,
    native_agents,
    product_agent_harnesses,
    valid_harnesses,
)
from omnigent.stores.global_instructions_store import GlobalInstructionRevision

_logger = logging.getLogger(__name__)

INSTRUCTION_SIZE_REMINDER_CHARS = 10_000


@dataclass(frozen=True)
class InstructionAgent:
    key: str
    display_name: str
    harnesses: tuple[str, ...]
    subject: tuple[str, str]

    def as_dict(self) -> dict[str, object]:
        return {
            "key": self.key,
            "display_name": self.display_name,
            "harnesses": list(self.harnesses),
            "subject": {"name": self.subject[0], "harness": self.subject[1]},
        }


def _canonical(harness: str) -> str:
    if harness.startswith("acp:"):
        return harness
    normalized = harness.replace("_", "-")
    return canonicalize_harness(normalized) or normalized


def instruction_agents() -> list[InstructionAgent]:
    """One descriptor set for editable layers, route previews and runtime selection."""
    natives = tuple(agent for agent in native_agents() if len(agent.key) <= 64)
    labels = harness_labels()
    harnesses = set(valid_harnesses())
    from omnigent.onboarding.acp_auth import acp_agents

    try:
        configured = acp_agents()
    except (OSError, ValueError):
        _logger.warning("instruction-agents/acp-config-unavailable")
        configured = []
    for agent in configured:
        harness = f"acp:{agent.slug}"
        harnesses.add(harness)
        labels[harness] = agent.name
    products = {agent.key: agent for agent in natives}
    by_product: dict[str, set[str]] = {agent.key: {agent.harness} for agent in natives}
    remaining = []
    for harness in sorted(harnesses):
        canonical = _canonical(harness)
        # Preserve the existing product layer for native and SDK spellings.
        product = canonical.removeprefix("native-").removesuffix("-native").removesuffix("-sdk")
        if product in products:
            by_product[product].add(canonical)
        else:
            remaining.append(canonical)
    result = [
        InstructionAgent(
            a.key,
            a.display_name,
            tuple(sorted(by_product[a.key], key=lambda h: (h != a.harness, h))),
            (a.agent_name, a.harness),
        )
        for a in natives
    ]
    product_names = {harness: name for name, harness in product_agent_harnesses().items()}
    seen = {harness for agent in result for harness in agent.harnesses}
    for harness in remaining:
        capability = capability_for_harness(harness)
        if (
            harness in seen
            or len(harness) > 64
            or not capability
            or capability.instruction_delivery
            in {InstructionDelivery.UNKNOWN, InstructionDelivery.NOT_DELIVERED}
        ):
            continue
        seen.add(harness)
        result.append(
            InstructionAgent(
                harness,
                labels.get(harness, harness),
                (harness,),
                (product_names.get(harness, harness), harness),
            )
        )
    return result


def agent_key_for_harness(harness: str | None) -> str | None:
    if harness is None:
        return None
    normalized = _canonical(harness)
    descriptors = instruction_agents()
    match = next((a.key for a in descriptors if normalized in a.harnesses), None)
    if match is not None:
        return match
    # Older sessions also used reversed native spellings not in the alias registry.
    product = normalized.removeprefix("native-").removesuffix("-native").removesuffix("-sdk")
    native_keys = {agent.key for agent in native_agents()}
    return next((a.key for a in descriptors if a.key == product and a.key in native_keys), None)


@dataclass(frozen=True)
class RouteInstructions:
    """The route text and its contributing, nonblank revisions in received order."""

    text: str
    revisions: tuple[GlobalInstructionRevision, ...]


def compose_route(layers: Sequence[GlobalInstructionRevision]) -> RouteInstructions:
    """Join shared → agent → host, unless a nonblank independent setting replaces them."""
    by_scope = {layer.scope: layer for layer in layers if layer.text.strip()}
    scopes = ("independent",) if "independent" in by_scope else ("shared", "agent", "host")
    revisions = tuple(by_scope[scope] for scope in scopes if scope in by_scope)
    return RouteInstructions("\n\n".join(layer.text for layer in revisions), revisions)


def compose_session_text(
    route_text: str,
    *,
    has_parent: bool,
    spawn: bool,
    mother_text: str = "",
    child_text: str = "",
    quota_line: str = "",
) -> str:
    """Append the session's role block, outside the route's independent override."""
    parts = [route_text]
    if has_parent:
        parts.append(child_text)
    elif spawn and mother_text.strip():
        parts.extend((mother_text, quota_line))
    return "\n\n".join(part for part in parts if part.strip())
