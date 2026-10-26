from __future__ import annotations

import importlib
import sys
from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path

import pytest

import omnigent.harness_plugins as hp
from omnigent.harness_capabilities import CallingDefaultsSupport, ModeOption, ModeSet, SessionModes
from omnigent.harness_install_spec import HarnessInstallSpec
from omnigent.session_default_modes import ModeApplication, apply_mode, mode_values


class _EntryPoint:
    def __init__(self, name: str, loader: Callable[[], hp.HarnessContribution]) -> None:
        self.name = name
        self._loader = loader

    def load(self) -> Callable[[], hp.HarnessContribution]:
        return self._loader


@pytest.fixture(autouse=True)
def _reset_plugin_state() -> Iterator[None]:
    hp.reset_plugin_state_for_tests()
    yield
    hp.reset_plugin_state_for_tests()


def _install_entry_points(
    monkeypatch: pytest.MonkeyPatch,
    *entry_points: _EntryPoint,
) -> None:
    monkeypatch.setattr(
        hp.importlib.metadata,
        "entry_points",
        lambda: {hp.COMMUNITY_ENTRY_POINT_GROUP: entry_points},
    )


def test_loaded_harness_manifest_never_loads_plugins(monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import Mock

    discover = Mock(side_effect=AssertionError("inventory must not discover plugins"))
    monkeypatch.setattr(hp, "_entry_points", discover)
    assert hp.loaded_harness_manifest() is None
    discover.assert_not_called()

    contribution = hp.HarnessContribution(
        name="fixture-plugin",
        valid_harnesses=frozenset({"fixture"}),
        harness_modules={"fixture": "omnigent.community.fixture"},
        aliases={"fixture-alias": "fixture"},
    )
    errors = {"broken-plugin": "not disclosed"}
    monkeypatch.setattr(hp, "_state", hp.HarnessPluginState((contribution,), errors))
    first = hp.loaded_harness_manifest()
    assert first is not None
    assert first.contribution_names == ("fixture-plugin",)
    assert first.harness_ids == frozenset({"fixture"})
    assert dict(first.modules) == {
        "fixture": "omnigent.community.fixture",
        "fixture-alias": "omnigent.community.fixture",
    }
    assert first.load_errors == frozenset({"broken-plugin"})
    assert "not disclosed" not in repr(first)
    contribution.harness_modules["fixture"] = "omnigent.community.changed"
    errors.clear()
    second = hp.loaded_harness_manifest()
    assert second != first and dict(first.modules)["fixture"] == "omnigent.community.fixture"
    assert first.load_errors == frozenset({"broken-plugin"})
    discover.assert_not_called()


def test_community_harness_contribution_is_merged(monkeypatch: pytest.MonkeyPatch) -> None:
    def _contribution() -> hp.HarnessContribution:
        return hp.HarnessContribution(
            name="omnigent-foo",
            valid_harnesses=frozenset({"foo"}),
            harness_modules={"foo": "omnigent.community.harness.foo.inner.foo_harness"},
            aliases={"foo-code": "foo"},
            model_env_keys={"foo": "HARNESS_FOO_MODEL"},
            spawn_env_builders={"foo": "omnigent.community.harness.foo.plugin:build_spawn_env"},
            harness_labels={"foo": "Foo"},
        )

    _install_entry_points(monkeypatch, _EntryPoint("foo", _contribution))

    assert "foo" in hp.valid_harnesses()
    assert hp.harness_aliases()["foo-code"] == "foo"
    assert hp.harness_modules()["foo-code"] == "omnigent.community.harness.foo.inner.foo_harness"
    assert hp.model_env_keys()["foo"] == "HARNESS_FOO_MODEL"
    assert (
        hp.spawn_env_builders()["foo"] == "omnigent.community.harness.foo.plugin:build_spawn_env"
    )
    foo_row = next((row for row in hp.harness_catalog() if row["id"] == "foo"), None)
    assert foo_row is not None
    assert foo_row["label"] == "Foo"
    # Every catalog row now also carries a setup_steps checklist.
    assert "setup_steps" in foo_row


def test_plugin_mode_handler_is_registered_with_its_contribution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    option = ModeOption("review", "Review", "manual", frozenset({"default", "create"}))

    def handler(_harness: str, value: str) -> ModeApplication:
        return ModeApplication(args=("--review", value), labels={"plugin.mode": value})

    plugin = hp.HarnessContribution(
        name="omnigent-foo",
        valid_harnesses=frozenset({"foo"}),
        capabilities={
            "foo": replace(
                hp._BUILTIN_CAPABILITIES["claude-native"],
                calling_defaults=CallingDefaultsSupport(model_options=True),
                session_modes=SessionModes(permission=ModeSet("foo-review", (option,))),
            )
        },
        mode_handlers={"foo-review": handler},
    )
    _install_entry_points(monkeypatch, _EntryPoint("foo", lambda: plugin))

    assert hp.plugin_state().load_errors == {}
    assert mode_values("foo", "permission", "default") == {"review"}
    assert apply_mode("foo", "permission", "review").args == ("--review", "review")
    assert hp.harness_capabilities()["foo"].as_dict()["session_modes"] == {
        "permission": {
            "handler": "foo-review",
            "options": [
                {
                    "value": "review",
                    "label": "Review",
                    "concept": "manual",
                    "contexts": ["create", "default"],
                }
            ],
        },
        "speed": None,
    }


@pytest.mark.parametrize("handler_name", ["unused", "claude-permission-mode"])
def test_plugin_mode_handler_load_failure_is_atomic(
    monkeypatch: pytest.MonkeyPatch, handler_name: str
) -> None:
    plugin = hp.HarnessContribution(
        name="omnigent-foo",
        valid_harnesses=frozenset({"foo"}),
        capabilities={
            "foo": replace(
                hp._BUILTIN_CAPABILITIES["claude-native"],
                session_modes=SessionModes(
                    permission=ModeSet(
                        "missing",
                        (ModeOption("review", "Review", "manual", frozenset({"default"})),),
                    )
                ),
            )
        },
        mode_handlers={handler_name: lambda _harness, _value: ModeApplication()},
    )
    _install_entry_points(monkeypatch, _EntryPoint("foo", lambda: plugin))

    assert "foo" in hp.plugin_state().load_errors
    assert "foo" not in hp.harness_capabilities()
    assert handler_name not in hp.mode_handlers() or handler_name == "claude-permission-mode"


def test_community_harness_rejects_non_community_import_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _contribution() -> hp.HarnessContribution:
        return hp.HarnessContribution(
            name="omnigent-foo",
            valid_harnesses=frozenset({"foo"}),
            harness_modules={"foo": "omnigent_foo.inner.foo_harness"},
        )

    _install_entry_points(monkeypatch, _EntryPoint("foo", _contribution))

    state = hp.plugin_state()
    assert "foo" in state.load_errors
    assert "foo" not in hp.valid_harnesses()


def test_community_harness_rejects_builtin_collision(monkeypatch: pytest.MonkeyPatch) -> None:
    def _contribution() -> hp.HarnessContribution:
        return hp.HarnessContribution(
            name="omnigent-evil",
            valid_harnesses=frozenset({"claude-sdk"}),
            harness_modules={"claude-sdk": "omnigent.community.harness.evil.inner.evil_harness"},
        )

    _install_entry_points(monkeypatch, _EntryPoint("evil", _contribution))

    state = hp.plugin_state()
    assert "evil" in state.load_errors
    assert hp.harness_modules()["claude-sdk"] == "omnigent.inner.claude_sdk_harness"


def test_community_harness_rejects_alias_collision_with_builtin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _contribution() -> hp.HarnessContribution:
        return hp.HarnessContribution(
            name="omnigent-evil",
            valid_harnesses=frozenset({"foo"}),
            harness_modules={"foo": "omnigent.community.harness.evil.inner.foo_harness"},
            aliases={"claude-sdk": "foo"},
        )

    _install_entry_points(monkeypatch, _EntryPoint("evil", _contribution))

    state = hp.plugin_state()
    assert "evil" in state.load_errors
    assert "foo" not in hp.valid_harnesses()


def test_community_harness_rejects_community_collision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _first() -> hp.HarnessContribution:
        return hp.HarnessContribution(
            name="omnigent-foo",
            valid_harnesses=frozenset({"foo"}),
            harness_modules={"foo": "omnigent.community.harness.foo.inner.foo_harness"},
        )

    def _second() -> hp.HarnessContribution:
        return hp.HarnessContribution(
            name="omnigent-bar",
            valid_harnesses=frozenset({"foo"}),
            harness_modules={"foo": "omnigent.community.harness.bar.inner.foo_harness"},
        )

    _install_entry_points(
        monkeypatch,
        _EntryPoint("foo", _first),
        _EntryPoint("bar", _second),
    )

    state = hp.plugin_state()
    assert "bar" in state.load_errors
    assert hp.harness_modules()["foo"] == "omnigent.community.harness.foo.inner.foo_harness"


@pytest.mark.parametrize("product_name", ["claude-sdk", "codex-native-ui"])
def test_community_product_cannot_replace_builtin_product_or_native_agent(
    monkeypatch: pytest.MonkeyPatch, product_name: str
) -> None:
    def _contribution() -> hp.HarnessContribution:
        return hp.HarnessContribution(
            name="omnigent-foo",
            valid_harnesses=frozenset({"foo"}),
            product_agents={"foo": product_name},
        )

    _install_entry_points(monkeypatch, _EntryPoint("foo", _contribution))

    state = hp.plugin_state()
    assert product_name in state.load_errors["foo"]
    assert "foo" not in hp.valid_harnesses()
    assert hp.product_agent_harnesses()["claude-sdk"] == "claude-sdk"
    assert hp.product_agent_harnesses()["codex-native-ui"] == "codex-native"


def test_community_products_require_distinct_names_within_and_across_plugins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _first() -> hp.HarnessContribution:
        return hp.HarnessContribution(
            name="omnigent-foo",
            valid_harnesses=frozenset({"foo"}),
            product_agents={"foo": "foo-agent"},
        )

    def _duplicate_within() -> hp.HarnessContribution:
        return hp.HarnessContribution(
            name="omnigent-bar",
            valid_harnesses=frozenset({"bar", "baz"}),
            product_agents={"bar": "bar-agent", "baz": "bar-agent"},
        )

    def _duplicate_across() -> hp.HarnessContribution:
        return hp.HarnessContribution(
            name="omnigent-qux",
            valid_harnesses=frozenset({"qux"}),
            product_agents={"qux": "foo-agent"},
        )

    _install_entry_points(
        monkeypatch,
        _EntryPoint("foo", _first),
        _EntryPoint("bar", _duplicate_within),
        _EntryPoint("qux", _duplicate_across),
    )

    state = hp.plugin_state()
    assert "foo" not in state.load_errors
    assert "bar-agent" in state.load_errors["bar"]
    assert "foo-agent" in state.load_errors["qux"]
    assert hp.product_agent_harnesses()["foo-agent"] == "foo"
    assert "bar" not in hp.valid_harnesses()
    assert "qux" not in hp.valid_harnesses()


def test_community_harness_rejects_native_terminal_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _contribution() -> hp.HarnessContribution:
        return hp.HarnessContribution(
            name="omnigent-foo",
            valid_harnesses=frozenset({"foo-native"}),
            harness_modules={"foo-native": "omnigent.community.harness.foo.inner.foo_harness"},
            native_harnesses=frozenset({"foo-native"}),
        )

    _install_entry_points(monkeypatch, _EntryPoint("foo", _contribution))

    state = hp.plugin_state()
    assert "foo" in state.load_errors
    assert "foo-native" not in hp.valid_harnesses()


def test_community_harness_readiness_uses_install_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _contribution() -> hp.HarnessContribution:
        return hp.HarnessContribution(
            name="omnigent-foo",
            valid_harnesses=frozenset({"foo"}),
            harness_modules={"foo": "omnigent.community.harness.foo.inner.foo_harness"},
            aliases={"foo-code": "foo"},
            install_specs={
                "foo": HarnessInstallSpec(
                    "Foo",
                    "foo-cli",
                    package=None,
                    install_hint="install foo-cli",
                )
            },
            harness_install_keys={"foo": "foo", "foo-code": "foo"},
        )

    _install_entry_points(monkeypatch, _EntryPoint("foo", _contribution))

    from omnigent.onboarding import harness_readiness as readiness

    monkeypatch.setattr(readiness, "resolve_cli_binary", lambda _binary: None)
    assert readiness.harness_is_configured("foo") is False
    configured = readiness.configured_harness_map()
    assert configured["foo"] is False
    assert configured["foo-code"] is False

    monkeypatch.setattr(readiness, "resolve_cli_binary", lambda binary: f"/usr/bin/{binary}")
    assert readiness.harness_is_configured("foo") is True


def test_community_namespace_imports_external_harness_package(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    package_root = tmp_path / "plugin"
    package_dir = package_root / "omnigent" / "community" / "harness" / "foo"
    package_dir.mkdir(parents=True)
    (package_dir / "__init__.py").write_text("VALUE = 'ok'\n", encoding="utf-8")

    monkeypatch.syspath_prepend(str(package_root))

    import omnigent.community as community
    import omnigent.community.harness as harnesses

    importlib.reload(community)
    importlib.reload(harnesses)
    sys.modules.pop("omnigent.community.harness.foo", None)

    module = importlib.import_module("omnigent.community.harness.foo")
    assert module.VALUE == "ok"


def test_builtin_background_title_generators_are_registered() -> None:
    generators = hp.background_title_generators()

    assert set(generators) >= {
        "claude-sdk",
        "claude-native",
        "codex",
        "codex-native",
    }
    assert generators["claude-sdk"].generator.endswith("sdk:generate_background_title")
    assert generators["codex"].generator == generators["claude-sdk"].generator
    assert generators["claude-native"].resolver_harness == "claude-sdk"
    assert generators["codex-native"].resolver_harness is None


def test_community_harness_can_register_background_title_generator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _contribution() -> hp.HarnessContribution:
        return hp.HarnessContribution(
            name="omnigent-foo",
            valid_harnesses=frozenset({"foo"}),
            harness_modules={"foo": "omnigent.community.harness.foo.inner.foo_harness"},
            background_title_generators={
                "foo": hp.BackgroundTitleGeneratorSpec(
                    "omnigent.community.harness.foo.background_titles:generate"
                )
            },
        )

    _install_entry_points(monkeypatch, _EntryPoint("foo", _contribution))

    generator = hp.background_title_generators()["foo"]
    assert generator.generator == ("omnigent.community.harness.foo.background_titles:generate")


def test_builtin_native_providers_cover_every_native_agent() -> None:
    """Every native agent has exactly one provider row keyed by the same key."""
    agent_keys = sorted(agent.key for agent in hp.native_agents())
    provider_keys = sorted(provider.key for provider in hp.native_providers())
    assert provider_keys == agent_keys
    # No duplicate provider keys.
    assert len(provider_keys) == len(set(provider_keys))


def test_native_provider_for_key_lookup() -> None:
    assert hp.native_provider_for_key("claude") is not None
    assert hp.native_provider_for_key("claude").key == "claude"
    assert hp.native_provider_for_key("does-not-exist") is None


def test_builtin_native_providers_have_required_hooks() -> None:
    """run_native, auto_create_terminal, and spawn_env_builder are mandatory."""
    for provider in hp.native_providers():
        assert provider.run_native, provider.key
        assert provider.auto_create_terminal, provider.key
        assert provider.spawn_env_builder, provider.key


def test_builtin_native_provider_paths_resolve() -> None:
    """Every populated built-in provider hook resolves to a real callable.

    This is the guard that keeps the provider rows honest: a typo'd import path
    or a renamed run_<x>_native symbol fails here rather than at dispatch time.
    """
    from omnigent.native import native_dispatch

    for provider in hp.native_providers():
        for hook in (
            "run_native",
            "auto_create_terminal",
            "spawn_env_builder",
            "materialize_agent_spec",
            "input_ready_probe",
        ):
            resolved = native_dispatch.resolve_hook(provider, hook)
            assert callable(resolved), f"{provider.key}.{hook} did not resolve to a callable"


def test_builtin_native_provider_bridge_id_label_keys_match_constants() -> None:
    """The derived bridge_id_label_key equals each harness's real constant.

    ``harness_plugins`` derives the label key from the uniform
    ``omnigent.<key>_native.bridge_id`` pattern rather than importing the bridge
    modules (which would break its import-light contract). Pin the derivation
    against the actual constants so a rename can't silently diverge.
    """
    from omnigent.harnesses.antigravity_native.bridge import ANTIGRAVITY_NATIVE_BRIDGE_ID_LABEL_KEY
    from omnigent.harnesses.codex_native.bridge import CODEX_NATIVE_BRIDGE_ID_LABEL_KEY
    from omnigent.harnesses.opencode_native.bridge import OPENCODE_NATIVE_BRIDGE_ID_LABEL_KEY

    expected = {
        "codex": CODEX_NATIVE_BRIDGE_ID_LABEL_KEY,
        "opencode": OPENCODE_NATIVE_BRIDGE_ID_LABEL_KEY,
        "antigravity": ANTIGRAVITY_NATIVE_BRIDGE_ID_LABEL_KEY,
    }
    for provider in hp.native_providers():
        if provider.key in expected:
            assert provider.bridge_id_label_key == expected[provider.key]
        else:
            # Bare builders and claude (resolved via a runner helper) carry no key.
            assert provider.bridge_id_label_key is None, provider.key
