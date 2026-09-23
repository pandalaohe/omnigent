"""The runner fixture ignores inherited environment pins."""

from __future__ import annotations

import os

import pytest

from tests.runner import conftest as runner_conftest


def test_inherited_auth_provider_cannot_override_root_pin(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> None:
    monkeypatch.setenv("OMNIGENT_AUTH_PROVIDER", "oidc")
    runner_conftest._hermetic_omnigent_env.__wrapped__(monkeypatch, tmp_path_factory)
    assert os.environ["OMNIGENT_AUTH_PROVIDER"] == "header"
    monkeypatch.setenv("OMNIGENT_AUTH_PROVIDER", "oidc")
    assert os.environ["OMNIGENT_AUTH_PROVIDER"] == "oidc"
