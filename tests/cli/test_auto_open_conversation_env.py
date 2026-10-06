"""Tests for the ``OMNIGENT_AUTO_OPEN_CONVERSATION`` env override."""

from __future__ import annotations

from typing import Any
from unittest.mock import Mock

import pytest
from click.testing import CliRunner

from omnigent.cli import (
    _AUTO_OPEN_CONVERSATION_ENV_VAR,
    _resolve_auto_open_conversation_from_config,
    _resolve_auto_open_conversation_preference,
    _resolve_auto_open_conversation_setting,
    cli,
)


@pytest.mark.parametrize(
    ("env_value", "cfg", "expected"),
    [
        ("false", {"auto_open_conversation": True}, False),
        ("0", {}, False),
        ("off", {}, False),
        ("true", {"auto_open_conversation": False}, True),
        (None, {}, None),
        ("", {"auto_open_conversation": True}, True),
        ("maybe", {}, None),
    ],
)
def test_resolver_env_override(
    monkeypatch: pytest.MonkeyPatch,
    env_value: str | None,
    cfg: dict[str, Any],
    expected: bool | None,
) -> None:
    """The env var wins over the config key; unset/blank/unknown falls through.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param env_value: Raw env var value, or ``None`` to leave it unset.
    :param cfg: Effective config dict passed to the resolver.
    :param expected: Expected tri-state resolver result.
    """
    if env_value is None:
        monkeypatch.delenv(_AUTO_OPEN_CONVERSATION_ENV_VAR, raising=False)
    else:
        monkeypatch.setenv(_AUTO_OPEN_CONVERSATION_ENV_VAR, env_value)

    assert _resolve_auto_open_conversation_preference(cfg) is expected


def test_config_resolver_ignores_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The config-only resolver ignores the env override.

    :param monkeypatch: Pytest monkeypatch fixture.
    """
    monkeypatch.setenv(_AUTO_OPEN_CONVERSATION_ENV_VAR, "false")

    assert _resolve_auto_open_conversation_setting({}) is None
    assert _resolve_auto_open_conversation_setting({"auto_open_conversation": True}) is True


@pytest.mark.parametrize(
    ("env_value", "expected"),
    [
        ("false", False),
        ("true", True),
    ],
)
def test_from_config_env_override(
    monkeypatch: pytest.MonkeyPatch,
    env_value: str,
    expected: bool,
) -> None:
    """The bool-defaulting wrapper returns the env override, key or not.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param env_value: Raw env var value to set.
    :param expected: Expected boolean result.
    """
    monkeypatch.setenv(_AUTO_OPEN_CONVERSATION_ENV_VAR, env_value)

    assert _resolve_auto_open_conversation_from_config({}) is expected


@pytest.mark.parametrize(
    ("env_value", "expected"),
    [
        ("false", False),
        (None, True),
    ],
)
def test_run_passes_env_override_through(
    monkeypatch: pytest.MonkeyPatch,
    env_value: str | None,
    expected: bool,
) -> None:
    """Interactive ``run`` dispatches the env override to ``run_chat``.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param env_value: Raw env var value, or ``None`` to leave it unset.
    :param expected: Expected ``auto_open_conversation`` kwarg.
    """
    if env_value is None:
        monkeypatch.delenv(_AUTO_OPEN_CONVERSATION_ENV_VAR, raising=False)
    else:
        monkeypatch.setenv(_AUTO_OPEN_CONVERSATION_ENV_VAR, env_value)
    monkeypatch.setattr("omnigent.cli._load_global_config", dict)
    run_chat = Mock()
    monkeypatch.setattr("omnigent.chat.run_chat", run_chat)

    result = CliRunner().invoke(
        cli,
        ["run", "tests/resources/examples/hello_world.yaml", "--harness", "codex", "--model", "m"],
    )

    assert result.exit_code == 0, result.output
    assert run_chat.call_args.kwargs["auto_open_conversation"] is expected
