"""Pure composition of route layers and exclusive session-role instructions."""

from collections.abc import Sequence
from dataclasses import dataclass

from omnigent.harness_aliases import canonicalize_harness
from omnigent.harness_plugins import native_agents
from omnigent.stores.global_instructions_store import GlobalInstructionRevision

INSTRUCTION_SIZE_REMINDER_CHARS = 10_000


def agent_key_for_harness(harness: str | None) -> str | None:
    """Resolve native/SDK spellings to a registered coding-agent product."""
    if harness is None:
        return None
    normalized = canonicalize_harness(harness.replace("_", "-")) or ""
    product = normalized.removeprefix("native-").removesuffix("-native").removesuffix("-sdk")
    return product if any(agent.key == product for agent in native_agents()) else None


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
