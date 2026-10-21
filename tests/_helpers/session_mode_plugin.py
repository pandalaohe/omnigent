"""A registered test harness for exercising launch modes through server entries."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from omnigent import harness_plugins as hp
from omnigent.harness_capabilities import CallingDefaultsSupport, ModeOption, ModeSet, SessionModes
from omnigent.session_default_modes import ModeApplication

TEST_MODE_HARNESS = "test-mode"


def install_test_mode_plugin(monkeypatch: Any) -> None:
    def apply_review(_harness: str, value: str) -> ModeApplication:
        return ModeApplication(args=("--review", value), labels={"plugin.mode": value})

    def apply_speed(_harness: str, value: str) -> ModeApplication:
        return ModeApplication(args=("--speed", value), labels={"plugin.speed": value})

    contribution = hp.HarnessContribution(
        name="omnigent-test-mode",
        valid_harnesses=frozenset({TEST_MODE_HARNESS}),
        harness_labels={TEST_MODE_HARNESS: "Test Mode"},
        capabilities={
            TEST_MODE_HARNESS: replace(
                hp._BUILTIN_CAPABILITIES["claude-native"],
                calling_defaults=CallingDefaultsSupport(model_options=True),
                session_modes=SessionModes(
                    permission=ModeSet(
                        "test-review",
                        (
                            ModeOption(
                                "review", "Review", "manual", frozenset({"default", "create"})
                            ),
                        ),
                    ),
                    speed=ModeSet(
                        "test-speed",
                        (
                            ModeOption(
                                "quiet", "Quiet", "default", frozenset({"default", "create"})
                            ),
                        ),
                    ),
                ),
            )
        },
        mode_handlers={"test-review": apply_review, "test-speed": apply_speed},
    )
    assert (
        hp._validate_community_contribution(
            contribution, entry_point_name="test-mode", existing=(hp._BUILTIN_CONTRIBUTION,)
        )
        is None
    )
    monkeypatch.setattr(
        hp,
        "plugin_state",
        lambda: hp.HarnessPluginState((hp._BUILTIN_CONTRIBUTION, contribution), {}),
    )
    # The spec parser snapshots accepted harnesses at import time. A real
    # installed entry point is present before that import; patch the snapshot.
    from omnigent.spec import _omnigent_compat

    monkeypatch.setattr(_omnigent_compat, "_OMNIGENT_ACCEPTED_HARNESSES", hp.accepted_harnesses())
    monkeypatch.setattr(_omnigent_compat, "OMNIGENT_HARNESSES", hp.valid_harnesses())
