"""Source-aware Pi native call classification, separate from ordinary tool policy."""

from __future__ import annotations

import json
import ntpath
import os
import re
from collections.abc import Iterable, Mapping
from pathlib import Path

from omnigent.permission_tiers import (
    NativeCallBinding,
    NativeInputSnapshot,
    NativeSupport,
    NativeSupportStatus,
    ToolIntent,
    ToolKind,
    TrustedHostContext,
    _absolute,
    _resolve,
)


def _native_rules() -> dict[str, frozenset[str]]:
    raw = json.loads(Path(__file__).with_name("native_support_rules.json").read_text())
    keys = {
        "run_keys",
        "option_keys",
        "step_keys",
        "chain_step_keys",
        "read_actions",
        "control_actions",
        "read_keys",
        "control_keys",
        "outside_kinds",
    }
    if (
        not isinstance(raw, dict)
        or type(raw.get("version")) is not int
        or raw["version"] != 1
        or set(raw) != keys | {"version"}
    ):
        raise ValueError("Unsupported native support rules")
    if any(
        not isinstance(raw[key], list)
        or not all(isinstance(item, str) and item for item in raw[key])
        for key in keys
    ):
        raise ValueError("Malformed native support rules")
    return {key: frozenset(raw[key]) for key in keys}


def _native_action(
    name: str, arguments: Mapping[str, object], context: TrustedHostContext
) -> str | None:
    rules = _native_rules()

    def cwd_matches(value: object) -> bool:
        if not isinstance(value, str) or not value.strip():
            return False
        module = ntpath if context.platform == "windows" else os.path
        lexical = module.normcase(module.normpath(_absolute(value, context)))
        workspace = module.normcase(module.normpath(context.workspace))
        return (
            lexical == workspace
            and _resolve(value, context).path == _resolve(context.workspace, context).path
        )

    def text_fields(value: Mapping[str, object], fields: Iterable[str]) -> bool:
        return all(
            key not in value or (isinstance(item := value[key], str) and bool(item.strip()))
            for key in fields
        )

    def integer(value: object, minimum: int = 1) -> bool:
        return isinstance(value, int) and not isinstance(value, bool) and value >= minimum

    if not cwd_matches(arguments.get("cwd", ".")):
        return None
    if name == "bg_wait":
        if set(arguments) - {"id", "nonBlocking", "all", "timeoutMs", "stopOnAttention"}:
            return None
        if not text_fields(arguments, ("id",)) or any(
            key in arguments and type(arguments[key]) is not bool
            for key in ("nonBlocking", "all", "stopOnAttention")
        ):
            return None
        if "timeoutMs" in arguments and (
            type(arguments["timeoutMs"]) is not int or arguments["timeoutMs"] < 1
        ):
            return None
        return "control"
    if name == "subagent_supervisor":
        return "control" if not arguments else None
    if name != "subagent":
        return None
    action = arguments.get("action")
    if isinstance(action, str) and (
        action in rules["read_actions"] or action in rules["control_actions"]
    ):
        category = "read" if action in rules["read_actions"] else "control"
        fields = rules["read_keys"] if category == "read" else rules["control_keys"]
        options = arguments.get("options", {})
        if (
            set(arguments) - fields
            or not isinstance(options, Mapping)
            or set(options) - {"agentScope", "topic"}
        ):
            return None
        if not text_fields(
            arguments, ("agent", "id", "runId", "message", "mode", "childId", "toolCallId", "view")
        ) or not text_fields(options, ("agentScope", "topic")):
            return None
        if any(
            key in arguments and not integer(arguments[key], 0 if key == "index" else 1)
            for key in ("index", "lines")
        ):
            return None
        if "capabilities" in arguments and type(arguments["capabilities"]) is not bool:
            return None
        if (
            arguments.get("view", "fleet") not in ("fleet", "transcript")
            or arguments.get("mode", "auto") not in ("steer", "follow_up", "auto")
            or options.get("agentScope", "both") not in ("user", "project", "both")
        ):
            return None
        return category
    if (
        "action" in arguments
        or set(arguments) - rules["run_keys"]
        or type(arguments.get("async")) is not bool
    ):
        return None
    options = arguments.get("options", {})
    if not isinstance(options, Mapping) or set(options) - rules["option_keys"]:
        return None
    if options.get("context", "fresh") not in ("fresh", "fork") or options.get(
        "agentScope", "both"
    ) not in ("user", "project", "both"):
        return None
    if any(
        key in options and (type(options[key]) is not int or options[key] < 1)
        for key in ("timeoutMs", "toolTimeoutMs")
    ):
        return None
    if "includeProgress" in options and type(options["includeProgress"]) is not bool:
        return None
    if "maxOutput" in options:
        output = options["maxOutput"]
        if (
            not isinstance(output, Mapping)
            or set(output) - {"bytes", "lines"}
            or any(type(value) is not int or value < 1 for value in output.values())
        ):
            return None
    forms = [key for key in ("agent", "tasks", "chain") if key in arguments]
    if len(forms) != 1:
        return None
    steps = [arguments] if forms[0] == "agent" else arguments[forms[0]]
    if not isinstance(steps, list) or not steps:
        return None
    for step in steps:
        if not isinstance(step, Mapping) or (
            forms[0] != "agent"
            and set(step)
            - (rules["chain_step_keys"] if forms[0] == "chain" else rules["step_keys"])
        ):
            return None
        launch = {**arguments, **step}
        if (
            not all(
                isinstance(value := launch.get(key), str) and value.strip()
                for key in ("agent", "task")
            )
            or not text_fields(launch, ("model", "as"))
            or not cwd_matches(launch.get("cwd", "."))
        ):
            return None
    return "delegate"


def adapt_native_call(
    name: str, arguments: Mapping[str, object], digest: str, context: TrustedHostContext
) -> ToolIntent | None:
    binding, support = context.native_binding, context.native_support

    def failed(reason: str) -> ToolIntent:
        return ToolIntent(
            ToolKind.UNKNOWN,
            name,
            digest,
            context,
            detail=reason,
            native_status=NativeSupportStatus.FAILED,
        )

    if binding is None and support is None and name not in context.native_tools:
        return None
    if (
        not isinstance(binding, NativeCallBinding)
        or not isinstance(support, NativeSupport)
        or support.binding != binding
        or binding.name != name
        or binding.arguments_digest != digest
        or type(binding.tier_revision) is not int
        or binding.tier_revision < 1
        or not all(
            isinstance(value, str) and value
            for value in (binding.scope_id, binding.installation_id, binding.registry_generation)
        )
    ):
        return failed("Native support proof is missing, malformed or stale")
    if support.status == NativeSupportStatus.FAILED:
        return failed(
            support.reason
            if isinstance(support.reason, str) and support.reason
            else "Native support proof failed"
        )
    if (
        not isinstance(support.reason, str)
        or not isinstance(support.inputs, tuple)
        or not support.inputs
        or not all(isinstance(item, NativeInputSnapshot) for item in support.inputs)
        or not isinstance(support.semantics_digest, str)
    ):
        return failed("Native input proof is malformed or incomplete")
    if support.action != "ordinary" or support.status == NativeSupportStatus.OUTSIDE:
        if not re.fullmatch(r"[0-9a-f]{64}", support.semantics_digest):
            return failed("Native semantic preflight digest is missing")
    path_module = ntpath if context.platform == "windows" else os.path
    for item in support.inputs:
        if not isinstance(item.path, str) or not path_module.isabs(item.path):
            return failed("Native input path is malformed")
        if item.sha256 is None:
            if item.resolved_path is not None:
                return failed("Native absent-input proof is malformed")
        elif (
            not isinstance(item.sha256, str)
            or not re.fullmatch(r"[0-9a-f]{64}", item.sha256)
            or not isinstance(item.resolved_path, str)
            or not path_module.isabs(item.resolved_path)
        ):
            return failed("Native input identity is malformed")
    if context.native_tools.get(name):
        return failed(context.native_tools[name])
    if support.status == NativeSupportStatus.OUTSIDE:
        if (
            name not in context.native_tools
            or not isinstance(support.outside_kind, str)
            or support.outside_kind not in _native_rules()["outside_kinds"]
            or not isinstance(support.reason, str)
            or not support.reason
        ):
            return failed("Outside-native support requires positive adapter evidence")
        return ToolIntent(
            ToolKind.UNKNOWN,
            name,
            digest,
            context,
            status="unknown",
            detail=support.reason,
            native_status=NativeSupportStatus.OUTSIDE,
        )
    if support.status != NativeSupportStatus.ATTESTED:
        return failed("Unknown native support classification")
    if name not in context.native_tools:
        return (
            None
            if support.action == "ordinary"
            else failed("Native tool action does not match its registration")
        )
    action = _native_action(name, arguments, context)
    if action is None or action != support.action:
        return failed("Native helper input, cwd or launch semantics are not attested")
    return ToolIntent(
        ToolKind.DELEGATE
        if action == "delegate"
        else ToolKind.READ
        if action == "read"
        else ToolKind.OTHER,
        name,
        digest,
        context,
        detail="framework-control" if action == "control" else "native-helper",
        native_status=NativeSupportStatus.ATTESTED,
    )
