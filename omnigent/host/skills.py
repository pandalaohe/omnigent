"""Host-owned menu discovery for directories and session agent bundles."""

from __future__ import annotations

import asyncio
import logging
import tempfile
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from pathlib import Path

import httpx

from omnigent.host.frames import HostSkillsFrame
from omnigent.spec import load
from omnigent.spec.skill_sources import (
    resolve_harness_skills,
    resolve_session_skills,
    skill_source_context_from_env,
)
from omnigent.spec.types import AgentSpec

_logger = logging.getLogger(__name__)

SKILLS_DISCOVERY_WAIT_SECONDS = 2.0
SKILLS_CACHE_TTL_SECONDS = 60.0
_MAX_CACHED_CATALOGS = 128


def _subagent_bundle(spec: AgentSpec, directory: Path, name: str) -> tuple[AgentSpec, Path]:
    """Find a child's actual bundle directory, including nested sub-agents."""
    if spec.name == name:
        return spec, directory
    for child in spec.sub_agents:
        segment = child.source_rel_dir
        if not segment or segment in (".", "..") or Path(segment).parts != (segment,):
            continue
        try:
            return _subagent_bundle(child, directory / "agents" / segment, name)
        except LookupError:
            continue
    raise LookupError(f"Sub-agent {name!r} is absent from the session bundle")


class HostSkillDiscovery:
    """Cache successful catalogs with one background scan per target."""

    def __init__(self, fetch_bundle: Callable[[HostSkillsFrame], httpx.Response]) -> None:
        self._fetch_bundle = fetch_bundle
        self._lock = threading.Lock()
        self._inflight: dict[tuple, asyncio.Task[list[dict[str, str]]]] = {}
        self._cache: OrderedDict[
            tuple[str | tuple[str, ...] | None, ...], tuple[float, list[dict[str, str]]]
        ] = OrderedDict()

    @staticmethod
    def _key(frame: HostSkillsFrame, root: Path) -> tuple:
        return (
            str(root),
            frame.harness,
            frame.session_id,
            frame.agent_id,
            frame.agent_version,
            frame.sub_agent_name,
            tuple(frame.skills_filter)
            if isinstance(frame.skills_filter, list)
            else frame.skills_filter,
        )

    async def discover_cached(self, frame: HostSkillsFrame, root: Path) -> list[dict[str, str]]:
        """Refresh once per target, bounding the wait only for opted-in pickers."""
        key = self._key(frame, root)
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                self._cache.move_to_end(key)
        if cached is not None and cached[0] > time.monotonic():
            return list(cached[1])
        task = self._inflight.get(key)
        if task is None:
            task = asyncio.create_task(asyncio.to_thread(self.discover, frame, root))
            self._inflight[key] = task

            def finished(done: asyncio.Task[list[dict[str, str]]]) -> None:
                self._inflight.pop(key, None)
                if not done.cancelled() and done.exception() is not None:
                    _logger.warning("Background skill discovery failed for %s", frame.harness)

            task.add_done_callback(finished)
        if cached is not None and frame.allow_pending:
            return list(cached[1])
        if frame.allow_pending:
            return await asyncio.wait_for(asyncio.shield(task), SKILLS_DISCOVERY_WAIT_SECONDS)
        return await asyncio.shield(task)

    async def shutdown(self) -> None:
        """Drain task wrappers; running filesystem scans finish on their worker."""
        tasks = list(self._inflight.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._inflight.clear()

    def discover(self, frame: HostSkillsFrame, root: Path) -> list[dict[str, str]]:
        """Return metadata for the exact launch target or effective session bundle."""
        key = self._key(frame, root)
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None and cached[0] > time.monotonic():
                self._cache.move_to_end(key)
                return list(cached[1])
        if frame.session_id is None:
            ctx = skill_source_context_from_env(
                roots=(root,), harness=frame.harness, skills_filter=frame.skills_filter
            )
            skills = resolve_harness_skills(ctx, frame.harness)
        else:
            response = self._fetch_bundle(frame)
            response.raise_for_status()
            if (
                response.headers.get("X-Agent-Id") != frame.agent_id
                or response.headers.get("X-Agent-Version") != frame.agent_version
            ):
                raise ValueError("The session's agent changed during discovery; retry")
            # Only metadata escapes this scope; invocation reads its own bundle.
            with tempfile.TemporaryDirectory(prefix="host-skill-bundle-") as tmp:
                directory = Path(tmp)
                spec = load(
                    response.content,
                    dest=directory,
                    expand_env=response.headers.get("X-Agent-Session-Scoped") == "false",
                    prune_invalid_sub_agents=True,
                )
                if frame.sub_agent_name:
                    spec, directory = _subagent_bundle(spec, directory, frame.sub_agent_name)
                skills = resolve_session_skills(spec, (root, directory), directory)
        result = [
            {"name": skill.name, "description": skill.description}
            | ({"display_name": skill.display_name} if skill.display_name else {})
            for skill in skills
        ]
        with self._lock:
            self._cache[key] = (time.monotonic() + SKILLS_CACHE_TTL_SECONDS, result)
            self._cache.move_to_end(key)
            while len(self._cache) > _MAX_CACHED_CATALOGS:
                self._cache.popitem(last=False)
            return list(result)
