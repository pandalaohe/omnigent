"""Owner-scoped starting prompts, resolved afresh at runner initialization."""

from __future__ import annotations

import hashlib
import json
import re
from typing import TYPE_CHECKING, Any

from omnigent.db.utils import builtin_agent_id
from omnigent.entities import Agent, Conversation
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.harness_capabilities import IntegrationMode
from omnigent.harness_plugins import capability_for_harness

if TYPE_CHECKING:
    from omnigent.runtime.agent_cache import AgentCache
    from omnigent.server.user_preferences_store import SqlAlchemyUserPreferencesStore
    from omnigent.spec import AgentSpec
    from omnigent.stores.agent_store import AgentStore

NAMESPACE = "builtin_agent_prompts"
PROMPT_MAX_CHARS = 262144
PROMPTS_MAX_BYTES = 4 * 1024 * 1024


def validate_prompt_preferences(value: Any) -> None:
    if not isinstance(value, dict) or len(value) > 16:
        raise ValueError("builtin_agent_prompts must contain at most 16 agents")
    for agent_id, row in value.items():
        if not isinstance(agent_id, str) or not re.fullmatch(r"[0-9a-f]{32}", agent_id):
            raise ValueError("builtin_agent_prompts requires agent ids")
        if not isinstance(row, dict) or set(row) != {"text", "base_sha256", "saved_at"}:
            raise ValueError("each built-in prompt needs text, base_sha256 and saved_at")
        if not isinstance(row["text"], str) or len(row["text"]) > PROMPT_MAX_CHARS:
            raise ValueError("built-in prompt exceeds the 262144 character limit")
        row["text"].encode("utf-8")
        if not isinstance(row["base_sha256"], str) or not re.fullmatch(
            r"[0-9a-f]{64}", row["base_sha256"]
        ):
            raise ValueError("base_sha256 must be a SHA-256 digest")
        if type(row["saved_at"]) is not int or row["saved_at"] < 0:
            raise ValueError("saved_at must be a nonnegative timestamp")
    if len(json.dumps(value, ensure_ascii=False).encode("utf-8")) > PROMPTS_MAX_BYTES:
        raise ValueError("builtin_agent_prompts exceeds the 4 MiB limit")


def prompt_spec(agent: Agent | None, cache: AgentCache) -> AgentSpec:
    if agent is None or not agent.operator_authored or agent.id != builtin_agent_id(agent.name):
        raise OmnigentError("Editable built-in prompt not found", code=ErrorCode.NOT_FOUND)
    spec = cache.load(agent.id, agent.bundle_location, expand_env=True).spec
    harness = spec.executor.config.get("harness") or spec.executor.type
    capability = capability_for_harness(harness)
    if capability is None or capability.integration_mode not in {
        IntegrationMode.SDK_IN_PROCESS,
        IntegrationMode.CLI_SUBPROCESS,
    }:
        raise OmnigentError("Editable built-in prompt not found", code=ErrorCode.NOT_FOUND)
    if spec.instructions is not None and not isinstance(spec.instructions, str):
        raise OmnigentError("This prompt cannot be edited as text", code=ErrorCode.CONFLICT)
    return spec


def prompt_view(
    agent: Agent, cache: AgentCache, preferences: SqlAlchemyUserPreferencesStore, owner: str
) -> dict[str, object]:
    spec = prompt_spec(agent, cache)
    text = spec.instructions or ""
    digest = hashlib.sha256(text.encode()).hexdigest()
    saved = (preferences.get(owner) or {}).get("settings", {}).get(NAMESPACE, {}).get(agent.id)
    return {
        "agent_id": agent.id,
        "text": saved["text"] if saved is not None else text,
        "builtin_text": text,
        "base_sha256": digest,
        "saved_base_sha256": saved["base_sha256"] if saved is not None else None,
        "customized": saved is not None,
        "builtin_changed": saved is not None and saved["base_sha256"] != digest,
    }


def session_instructions_override(
    conversation: Conversation,
    *,
    owner: str,
    agent_store: AgentStore,
    agent_cache: AgentCache,
    preferences: SqlAlchemyUserPreferencesStore | None,
) -> str | None:
    if preferences is None or conversation.agent_id is None or conversation.sub_agent_name:
        return None
    template = agent_store.get_template_ids([conversation.agent_id]).get(
        conversation.agent_id, conversation.agent_id
    )
    agent = agent_store.get(template)
    if agent is None or not agent.operator_authored or agent.id != builtin_agent_id(agent.name):
        return None
    saved = (preferences.get(owner) or {}).get("settings", {}).get(NAMESPACE, {}).get(agent.id)
    if saved is None:
        return None
    prompt_spec(agent, agent_cache)
    return saved["text"]
