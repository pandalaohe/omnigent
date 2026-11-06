"""Claude Code's context headroom, shared by native and SDK reporting."""

from __future__ import annotations

import json
import math
import os
from collections.abc import Collection, Mapping
from pathlib import Path

from omnigent.models.claude_model_vocabulary import normalized_model_id


def claude_auto_compact_token_limit(
    context_window: int,
    *,
    settings: Mapping[str, object],
    env: Mapping[str, str],
    model: str | None = None,
    max_output_tokens: int = 20_000,
) -> int | None:
    """Resolve the threshold, including Claude's output and summary reserves."""
    settings_env = settings.get("env")
    effective_env = {**env, **(settings_env if isinstance(settings_env, dict) else {})}
    if settings.get("autoCompactEnabled") is False or any(
        str(effective_env.get(key, "")).lower() in {"1", "true", "yes", "on"}
        for key in ("DISABLE_AUTO_COMPACT", "DISABLE_COMPACT")
    ):
        return None
    window = settings.get("autoCompactWindow")
    model_settings = settings.get("modelSettings")
    if model and isinstance(model_settings, dict):
        for key, value in model_settings.items():
            if normalized_model_id(str(key)) == normalized_model_id(model) and isinstance(
                value, dict
            ):
                window = value.get("autoCompactWindow", window)
                break
    configured = effective_env.get("CLAUDE_CODE_AUTO_COMPACT_WINDOW")
    if isinstance(configured, str):
        try:
            parsed = int(configured.strip())
        except ValueError:
            parsed = 0
        if parsed > 0:
            window = parsed
    if not isinstance(window, int) or isinstance(window, bool) or window <= 0:
        # Unconfigured defaults can come from model-specific or experiment state
        # that is absent from statusLine and modelUsage.
        return None
    context_window = min(context_window, window)
    usable_window = context_window - min(max_output_tokens, 20_000)
    threshold = usable_window - 13_000
    try:
        percent = float(str(effective_env.get("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE", "")))
    except ValueError:
        percent = 0
    if math.isfinite(percent) and 0 < percent <= 100:
        threshold = min(threshold, math.floor(usable_window * percent / 100))
    return threshold if threshold > 0 else None


def read_claude_auto_compact_token_limit(
    context_window: int,
    *,
    model: str | None = None,
    cwd: Path | None = None,
    max_output_tokens: int = 20_000,
    setting_sources: Collection[str] | None = None,
    env: Mapping[str, str] | None = None,
) -> int | None:
    """Read the settings the CLI loads without changing its configuration.

    :param setting_sources: The CLI's ``--setting-sources``, e.g. ``[]`` when it
        loads no settings file; ``None`` means its default, user, project and local.
    :param env: The CLI's launch environment; ``None`` means this process's.
    """
    working_dir = cwd or Path.cwd()
    config_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    if not config_dir.is_absolute():
        config_dir = working_dir / config_dir
    sources = {"user", "project", "local"} if setting_sources is None else set(setting_sources)
    settings: dict[str, object] = {}
    for source, path in (
        (
            None,
            config_dir / ".claude.json"
            if os.environ.get("CLAUDE_CONFIG_DIR")
            else Path.home() / ".claude.json",
        ),
        ("user", config_dir / "settings.json"),
        ("project", working_dir / ".claude/settings.json"),
        ("local", working_dir / ".claude/settings.local.json"),
    ):
        if source is not None and source not in sources:
            continue
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError):
            continue
        if not isinstance(value, dict):
            continue
        if "autoCompactWindow" in value:
            settings.pop("modelSettings", None)
        for key in ("env", "modelSettings"):
            before, after = settings.get(key), value.get(key)
            if isinstance(before, dict) and isinstance(after, dict):
                value[key] = {**before, **after}
        settings.update(value)
    return claude_auto_compact_token_limit(
        context_window,
        settings=settings,
        env=os.environ if env is None else env,
        model=model,
        max_output_tokens=max_output_tokens,
    )
