"""Host-daemon environment boundary regression tests."""

from __future__ import annotations

import os
from typing import Final

import pytest

from omnigent.cli import _build_host_daemon_env
from omnigent.host.connect import (
    RUNNER_ENV_PASSTHROUGH_ENV_VAR,
    _build_runner_env,
)

_REMOTE_SERVER_URL: Final = "https://example.databricksapps.com"
_PROXY_ENV: Final = {
    "HTTP_PROXY": "http://upper-http-proxy.example.com:3128",
    "HTTPS_PROXY": "http://upper-https-proxy.example.com:3128",
    "ALL_PROXY": "socks5://upper-proxy.example.com:1080",
    "NO_PROXY": "localhost,127.0.0.1",
    "http_proxy": "http://lower-http-proxy.example.com:3128",
    "https_proxy": "http://lower-https-proxy.example.com:3128",
    "all_proxy": "socks5://lower-proxy.example.com:1080",
    "no_proxy": "localhost,127.0.0.2",
}


@pytest.mark.parametrize(
    ("server_url", "keeps_provider_secret"),
    [(None, True), (_REMOTE_SERVER_URL, False)],
)
def test_host_daemon_env_preserves_proxy_vars_and_provider_secret_split(
    monkeypatch: pytest.MonkeyPatch,
    server_url: str | None,
    keeps_provider_secret: bool,
) -> None:
    """Proxy selectors reach both daemon modes without widening provider secrets."""
    # Given
    for name, value in _PROXY_ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("DATABRICKS_CONFIG_PROFILE", "corp")
    monkeypatch.setenv("OPENAI_API_KEY", "local-provider-secret")

    # When
    env = _build_host_daemon_env(server_url=server_url)

    # Then
    # Windows environment keys are case-insensitive, so upper- and lower-case
    # spellings cannot hold distinct values there. Assert against the proxy
    # variables the current OS can actually represent; POSIX still exercises
    # all eight spellings and values.
    actual_proxy_env = {
        name: value
        for name, value in os.environ.items()
        if name.upper() in {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"}
    }
    assert {name: env.get(name) for name in actual_proxy_env} == actual_proxy_env
    assert env["DATABRICKS_CONFIG_PROFILE"] == "corp"
    assert ("OPENAI_API_KEY" in env) is keeps_provider_secret


def test_runner_env_excludes_proxy_vars_by_default() -> None:
    """Daemon proxy credentials do not cross into runner subprocesses by default."""
    # Given
    base_env = {"PATH": "/usr/bin", **_PROXY_ENV}

    # When
    env = _build_runner_env(
        base_env,
        server_url=_REMOTE_SERVER_URL,
        runner_id="runner_proxy",
        binding_token="binding-proxy",
        workspace="/tmp/workspace",
        parent_pid=12345,
    )

    # Then
    assert set(_PROXY_ENV).isdisjoint(env)


def test_runner_env_explicit_proxy_passthrough_remains_available() -> None:
    """Runner proxy forwarding remains opt-in through the existing passthrough."""
    # Given
    explicit_names = {"HTTPS_PROXY", "http_proxy"}
    base_env = {
        "PATH": "/usr/bin",
        **_PROXY_ENV,
        RUNNER_ENV_PASSTHROUGH_ENV_VAR: ",".join(explicit_names),
    }

    # When
    env = _build_runner_env(
        base_env,
        server_url=_REMOTE_SERVER_URL,
        runner_id="runner_proxy",
        binding_token="binding-proxy",
        workspace="/tmp/workspace",
        parent_pid=12345,
    )

    # Then
    assert {name: env[name] for name in explicit_names} == {
        name: _PROXY_ENV[name] for name in explicit_names
    }
    assert (set(_PROXY_ENV) - explicit_names).isdisjoint(env)


_CLAUDE_TOOL_SEARCH_ENV: Final = {
    "CLAUDE_CODE_USE_GATEWAY": "1",
    "ENABLE_TOOL_SEARCH": "true",
}


@pytest.mark.parametrize("server_url", [None, _REMOTE_SERVER_URL])
def test_host_daemon_env_preserves_claude_tool_search_flags(
    monkeypatch: pytest.MonkeyPatch,
    server_url: str | None,
) -> None:
    """USE_GATEWAY / ENABLE_TOOL_SEARCH reach the daemon in both modes (Gate 1)."""
    # Given
    for name, value in _CLAUDE_TOOL_SEARCH_ENV.items():
        monkeypatch.setenv(name, value)

    # When
    env = _build_host_daemon_env(server_url=server_url)

    # Then
    assert {name: env.get(name) for name in _CLAUDE_TOOL_SEARCH_ENV} == _CLAUDE_TOOL_SEARCH_ENV


def test_runner_env_preserves_claude_tool_search_flags() -> None:
    """USE_GATEWAY / ENABLE_TOOL_SEARCH reach the runner subprocess (Gate 2)."""
    # Given
    base_env = {"PATH": "/usr/bin", **_CLAUDE_TOOL_SEARCH_ENV}

    # When
    env = _build_runner_env(
        base_env,
        server_url=_REMOTE_SERVER_URL,
        runner_id="runner_tool_search",
        binding_token="binding-tool-search",
        workspace="/tmp/workspace",
        parent_pid=12345,
    )

    # Then
    assert {name: env.get(name) for name in _CLAUDE_TOOL_SEARCH_ENV} == _CLAUDE_TOOL_SEARCH_ENV


# The gcloud ADC auth selectors the Antigravity CLI (agy) reads. Non-secret
# selectors/paths (the ADC file *contains* the credential; the vars just point
# at it), so they ride the allowlists like KUBECONFIG rather than the
# credential passthrough. The CLOUDSDK_ entries are exact names on purpose:
# gcloud's CLOUDSDK_AUTH_* vars carry live tokens and must stay stripped.
_GCLOUD_ADC_ENV: Final = {
    "AGY_ADC_AUTH": "true",
    "GOOGLE_APPLICATION_CREDENTIALS": "/home/alice/.config/gcloud/adc.json",
    "GOOGLE_CLOUD_PROJECT": "acme-dev",
    "GOOGLE_CLOUD_QUOTA_PROJECT": "acme-quota",
    "CLOUDSDK_CONFIG": "/home/alice/.config/gcloud",
    "CLOUDSDK_ACTIVE_CONFIG_NAME": "alt",
}


@pytest.mark.parametrize("server_url", [None, _REMOTE_SERVER_URL])
def test_host_daemon_env_preserves_gcloud_adc_selectors(
    monkeypatch: pytest.MonkeyPatch,
    server_url: str | None,
) -> None:
    """The gcloud ADC selectors survive the CLI→daemon strip in both modes.

    Without them the detached daemon loses the user's gcloud login, so every
    antigravity-native pane it (transitively) spawns blocks at agy's
    interactive "Select login method" menu.
    """
    # Given
    for name, value in _GCLOUD_ADC_ENV.items():
        monkeypatch.setenv(name, value)

    # When
    env = _build_host_daemon_env(server_url=server_url)

    # Then
    assert {name: env.get(name) for name in _GCLOUD_ADC_ENV} == _GCLOUD_ADC_ENV


def test_runner_env_preserves_gcloud_adc_selectors() -> None:
    """The gcloud ADC selectors survive the daemon→runner strip.

    The runner env is what the antigravity-native pane ultimately inherits,
    so this hop is where a drop turns into agy's login menu.
    """
    # Given
    base_env = {"PATH": "/usr/bin", **_GCLOUD_ADC_ENV}

    # When
    env = _build_runner_env(
        base_env,
        server_url=_REMOTE_SERVER_URL,
        runner_id="runner_adc",
        binding_token="binding-adc",
        workspace="/tmp/workspace",
        parent_pid=12345,
    )

    # Then
    assert {name: env.get(name) for name in _GCLOUD_ADC_ENV} == _GCLOUD_ADC_ENV


# gcloud vars that hold live credentials, not selectors. They must NOT ride
# the allowlists: a CLOUDSDK_ prefix entry would hand usable GCP tokens to the
# runner (and transitively the agent-controlled harness).
_GCLOUD_TOKEN_ENV: Final = {
    "CLOUDSDK_AUTH_ACCESS_TOKEN": "ya29.fake-access-token",
    "CLOUDSDK_AUTH_REFRESH_TOKEN": "1//fake-refresh-token",
}


@pytest.mark.parametrize("server_url", [None, _REMOTE_SERVER_URL])
def test_host_daemon_env_strips_gcloud_auth_tokens(
    monkeypatch: pytest.MonkeyPatch,
    server_url: str | None,
) -> None:
    """gcloud CLOUDSDK_AUTH_* bearer/refresh tokens do NOT reach the daemon."""
    # Given
    for name, value in _GCLOUD_TOKEN_ENV.items():
        monkeypatch.setenv(name, value)

    # When
    env = _build_host_daemon_env(server_url=server_url)

    # Then
    assert not set(_GCLOUD_TOKEN_ENV) & set(env)


def test_runner_env_strips_gcloud_auth_tokens() -> None:
    """gcloud CLOUDSDK_AUTH_* bearer/refresh tokens do NOT reach the runner."""
    # Given
    base_env = {"PATH": "/usr/bin", **_GCLOUD_TOKEN_ENV}

    # When
    env = _build_runner_env(
        base_env,
        server_url=_REMOTE_SERVER_URL,
        runner_id="runner_adc_tokens",
        binding_token="binding-adc-tokens",
        workspace="/tmp/workspace",
        parent_pid=12345,
    )

    # Then
    assert not set(_GCLOUD_TOKEN_ENV) & set(env)


@pytest.mark.parametrize("server_url", [None, _REMOTE_SERVER_URL])
def test_host_daemon_env_defaults_pythonutf8_on(
    monkeypatch: pytest.MonkeyPatch,
    server_url: str | None,
) -> None:
    """Both daemon modes force UTF-8 mode so status glyphs can't kill stdio."""
    monkeypatch.delenv("PYTHONUTF8", raising=False)

    env = _build_host_daemon_env(server_url=server_url)

    assert env.get("PYTHONUTF8") == "1"


@pytest.mark.parametrize("server_url", [None, _REMOTE_SERVER_URL])
def test_host_daemon_env_keeps_explicit_pythonutf8(
    monkeypatch: pytest.MonkeyPatch,
    server_url: str | None,
) -> None:
    """An explicit user PYTHONUTF8 value stays authoritative over the default."""
    monkeypatch.setenv("PYTHONUTF8", "0")

    env = _build_host_daemon_env(server_url=server_url)

    assert env.get("PYTHONUTF8") == "0"
