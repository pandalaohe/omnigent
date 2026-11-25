"""Usage-telemetry opt-outs shared by the server client and local launcher."""

from __future__ import annotations

from collections.abc import Mapping

DISABLE_TELEMETRY_ENV_VAR = "OMNIGENT_DISABLE_TELEMETRY"
# Usage telemetry is opt-in in this distribution; the opt-outs below still win.
ENABLE_TELEMETRY_ENV_VAR = "OMNIGENT_ENABLE_TELEMETRY"
CI_ENV_VARS = frozenset(
    {
        "CI",
        "GITHUB_ACTIONS",
        "PYTEST_CURRENT_TEST",
        "CIRCLECI",
        "JENKINS_URL",
        "TRAVIS",
        "GITLAB_CI",
        "TF_BUILD",
        "BITBUCKET_BUILD_NUMBER",
        "CODEBUILD_BUILD_ARN",
        "BUILDKITE",
        "TEAMCITY_VERSION",
    }
)
TELEMETRY_OPT_OUT_ENV_VARS = frozenset(
    {
        DISABLE_TELEMETRY_ENV_VAR,
        "DISABLE_TELEMETRY",
        "DO_NOT_TRACK",
        "OMNIGENT_ANALYTICS",
        *CI_ENV_VARS,
    }
)
ZEROBUS_DESTINATION_ENV_VARS = frozenset(
    {
        "OMNIGENT_DEBUG_LOG_CLIENT_ID",
        "OMNIGENT_DEBUG_LOG_CLIENT_SECRET",
        "OMNIGENT_DEBUG_LOG_CLIENT_SECRET_COMMAND",
        "OMNIGENT_DEBUG_LOG_WORKSPACE_URL",
        "OMNIGENT_DEBUG_LOG_ENDPOINT",
    }
)
_TELEMETRY_CONFIG_ENV_VARS = frozenset(
    {
        "OMNIGENT_CONFIG",
        "OMNIGENT_CONFIG_HOME",
        "OMNIGENT_ADMIN_CREDENTIALS_PATH",
    }
)


def preserve_zerobus_opt_outs(env: dict[str, str], source_env: Mapping[str, str]) -> None:
    """Carry deployment telemetry opt-outs whenever a ZeroBus destination passes."""
    if not any(env.get(name) for name in ZEROBUS_DESTINATION_ENV_VARS):
        return
    for name in TELEMETRY_OPT_OUT_ENV_VARS | _TELEMETRY_CONFIG_ENV_VARS:
        if name in source_env:
            env[name] = source_env[name]


def is_disabled_by_env(env: Mapping[str, str]) -> bool:
    """Check usage-telemetry opt-in and opt-outs without reading config or cached state."""
    return telemetry_override(env) != (True, ENABLE_TELEMETRY_ENV_VAR)


def telemetry_override(
    env: Mapping[str, str], *, disabled_by_config: bool = False
) -> tuple[bool, str] | None:
    """Return the deployment's locked value and source, if any."""
    truthy = ("1", "true", "yes")
    for name in (DISABLE_TELEMETRY_ENV_VAR, "DISABLE_TELEMETRY"):
        if env.get(name, "").strip().lower() in truthy:
            return False, name
    if env.get("DO_NOT_TRACK", "").strip() == "1":
        return False, "DO_NOT_TRACK"
    if env.get("OMNIGENT_ANALYTICS", "").strip() == "0":
        return False, "OMNIGENT_ANALYTICS"
    for name in sorted(CI_ENV_VARS):
        if name in env:
            return False, name
    if disabled_by_config:
        return False, "config.yaml"
    if env.get(ENABLE_TELEMETRY_ENV_VAR, "").strip().lower() in truthy:
        return True, ENABLE_TELEMETRY_ENV_VAR
    return None
