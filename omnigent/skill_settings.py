"""Read host-level skill settings from the user config."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import yaml

from omnigent.config import global_config_path, load_global_config

_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class HostSkillSettings:
    """Host-level skill settings parsed from the user config.

    :param trusted_link_roots: Resolved absolute directories under which
        a skill's symlink target may be read.
    :param claude_portable_skills: Whether Claude-family discovery may
        include ``.agents/skills`` tiers.
    """

    trusted_link_roots: tuple[Path, ...] = ()
    claude_portable_skills: bool = True


def _trusted_link_roots(raw: object) -> tuple[Path, ...]:
    """Parse ``skills.trusted_link_roots``, failing closed to ``()``.

    :param raw: The raw ``skills.trusted_link_roots`` value.
    :returns: The resolved roots, or ``()`` with one warning when any
        entry is not an absolute path that can be expanded and resolved.
    """
    if raw is None:
        return ()
    if not isinstance(raw, list) or not all(isinstance(item, str) and item for item in raw):
        _logger.warning("Ignoring skills.trusted_link_roots: expected a list of non-empty strings")
        return ()

    roots: list[Path] = []
    for item in raw:
        try:
            root = Path(item).expanduser()
        except (OSError, RuntimeError, ValueError) as err:
            _logger.warning("Ignoring skills.trusted_link_roots: cannot expand %r: %s", item, err)
            return ()
        if not root.is_absolute():
            _logger.warning("Ignoring skills.trusted_link_roots: %r is not absolute", item)
            return ()
        try:
            roots.append(root.resolve())
        except (OSError, RuntimeError, ValueError) as err:
            _logger.warning("Ignoring skills.trusted_link_roots: cannot resolve %r: %s", item, err)
            return ()

    return tuple(roots)


def host_skill_settings() -> HostSkillSettings:
    """Read the host skill settings from the user config.

    Never raises: an unusable config location, an unreadable or
    undecodable file, or a non-mapping config logs one warning and
    yields all defaults; an invalid individual value logs one warning
    and yields only that field's default.

    :returns: The validated host skill settings.
    """
    config_path = global_config_path()
    if not config_path.is_absolute():
        _logger.warning(
            "Ignoring skills.trusted_link_roots: config path is not absolute: %s",
            config_path,
        )
        return HostSkillSettings()

    try:
        cfg = load_global_config()
    # RuntimeError covers RecursionError from pathologically nested YAML.
    except (OSError, RuntimeError, ValueError, yaml.YAMLError) as err:
        _logger.warning("Ignoring skills.trusted_link_roots: unreadable config: %s", err)
        return HostSkillSettings()

    if not isinstance(cfg, dict):
        _logger.warning("Ignoring skills.trusted_link_roots: config is not a mapping")
        return HostSkillSettings()
    block = cfg.get("skills")
    if block is None:
        return HostSkillSettings()
    if not isinstance(block, dict):
        _logger.warning("Ignoring skills.trusted_link_roots: 'skills' is not a mapping")
        return HostSkillSettings()

    trusted_link_roots = _trusted_link_roots(block.get("trusted_link_roots"))
    if "claude_portable_skills" not in block:
        claude_portable_skills = True
    else:
        raw_portable = block["claude_portable_skills"]
        if isinstance(raw_portable, bool):
            claude_portable_skills = raw_portable
        else:
            _logger.warning(
                "Ignoring skills.claude_portable_skills: expected a boolean, got %s",
                type(raw_portable).__name__,
            )
            claude_portable_skills = True

    return HostSkillSettings(
        trusted_link_roots=trusted_link_roots,
        claude_portable_skills=claude_portable_skills,
    )
