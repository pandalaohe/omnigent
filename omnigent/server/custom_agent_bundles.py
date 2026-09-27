"""Scalar edits retain unrelated archive members; member edits re-dump touched configs."""

from __future__ import annotations

import copy
import io
import json
import re
import tarfile
import tempfile
import uuid
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.spec import AgentSpec, extract_safe
from omnigent.spec.parser import _ConfigYamlLoader

MAX_BUNDLE_BYTES = 32 * 1024 * 1024
_MANAGED_INSTRUCTIONS_PATH = re.compile(r"catalog-instructions-[0-9a-f]{32}\.md")


def project_members(spec: AgentSpec) -> list[dict[str, Any]]:
    remaining = list(spec.sub_agents)
    ordered: list[AgentSpec] = []
    for name in spec.tools.agents:
        for index, member in enumerate(remaining):
            if member.name == name:
                ordered.append(remaining.pop(index))
                break
    ordered.extend(remaining)
    return [
        {
            "name": member.name,
            "description": member.description,
            "harness": member.executor.harness_kind,
            "model": member.executor.model,
            "reasoning_effort": member.executor.reasoning_effort,
            "lead": index == 0,
        }
        for index, member in enumerate((spec, *ordered))
    ]


def _managed_instructions_path(raw: str) -> str | None:
    """Return our existing generated instructions member, when present."""
    node = yaml.compose(raw)
    if not isinstance(node, yaml.MappingNode):
        return None
    for key, value in node.value:
        if (
            key.value == "instructions"
            and isinstance(value, yaml.ScalarNode)
            and _MANAGED_INSTRUCTIONS_PATH.fullmatch(value.value)
        ):
            return value.value
    return None


def _patch_yaml_fields(raw: str, fields: dict[str, Any]) -> str:
    """Edit scalar values while preserving unrelated YAML and alias values."""
    node = yaml.compose(raw)
    if not isinstance(node, yaml.MappingNode):
        raise OmnigentError("Agent configuration must be a mapping", code=ErrorCode.INVALID_INPUT)
    tokens = list(yaml.scan(raw))
    edits: list[tuple[int, int, str]] = []
    found: set[str] = set()
    detached_anchors: dict[str, str] = {}
    for index, (key, value) in enumerate(node.value):
        if key.value not in fields:
            continue
        if key.value in found:
            raise OmnigentError(
                "Duplicate editable configuration key", code=ErrorCode.INVALID_INPUT
            )
        found.add(key.value)
        end = (
            node.value[index + 1][0].start_mark.index
            if index + 1 < len(node.value)
            else node.end_mark.index
        )
        alias = next(
            (
                token
                for token in tokens
                if isinstance(token, yaml.AliasToken)
                and key.end_mark.index <= token.start_mark.index < end
            ),
            None,
        )
        if alias is not None:
            start, stop = alias.start_mark.index, alias.end_mark.index
        else:
            start, stop = value.start_mark.index, value.end_mark.index
            for token in tokens:
                if isinstance(token, yaml.AnchorToken) and start <= token.start_mark.index < stop:
                    if not isinstance(value, yaml.ScalarNode):
                        raise OmnigentError(
                            "Editable fields must be scalars", code=ErrorCode.INVALID_INPUT
                        )
                    original = copy.copy(value)
                    original.style = '"'
                    detached_anchors[token.value] = yaml.serialize(original).strip()
        replacement = json.dumps(fields[key.value], ensure_ascii=False)
        if raw[start:stop].endswith("\n"):
            replacement += "\n"
        edits.append((start, stop, replacement))
    # Detach references to replaced anchors so other fields retain their values.
    for token in tokens:
        if isinstance(token, yaml.AliasToken) and token.value in detached_anchors:
            if not any(start <= token.start_mark.index < stop for start, stop, _ in edits):
                edits.append(
                    (token.start_mark.index, token.end_mark.index, detached_anchors[token.value])
                )
    missing = fields.keys() - found
    if missing:
        additions = [
            f"{json.dumps(key)}: {json.dumps(fields[key], ensure_ascii=False)}"
            for key in sorted(missing)
        ]
        if node.flow_style:
            close = node.end_mark.index - 1
            before_close = [token for token in tokens if token.end_mark.index <= close]
            trailing_comma = bool(before_close) and isinstance(
                before_close[-1], yaml.FlowEntryToken
            )
            separator = ", " if node.value and not trailing_comma else " "
            edits.append((close, close, separator + ", ".join(additions)))
        else:
            end = node.end_mark.index
            separator = "" if raw[:end].endswith("\n") else "\n"
            edits.append((end, end, separator + "\n".join(additions) + "\n"))
    for start, stop, replacement in sorted(edits, reverse=True):
        raw = raw[:start] + replacement + raw[stop:]
    return raw


def _load_mapping(raw: str) -> dict[str, Any]:
    data = yaml.load(raw, Loader=_ConfigYamlLoader)
    if not isinstance(data, dict):
        raise OmnigentError("Agent configuration must be a mapping", code=ErrorCode.INVALID_INPUT)
    return data


def _dump_mapping(data: dict[str, Any]) -> bytes:
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True).encode("utf-8")


def _mutable_mapping(data: dict[str, Any], key: str) -> dict[str, Any]:
    """Replace ``data[key]`` with a copy before editing: aliases load as shared objects."""
    value = data.get(key)
    value = dict(value) if isinstance(value, dict) else {}
    data[key] = value
    return value


def _set_or_remove(data: dict[str, Any], key: str, value: Any) -> None:
    if value is None:
        data.pop(key, None)
    else:
        data[key] = value


def _apply_member(data: dict[str, Any], member: dict[str, Any]) -> None:
    """Fold one member's description and executor into a parsed config mapping."""
    _set_or_remove(data, "description", member.get("description"))
    executor = _mutable_mapping(data, "executor")
    _mutable_mapping(executor, "config")["harness"] = member["harness"]
    for key in ("model", "reasoning_effort"):
        _set_or_remove(executor, key, member.get(key))
    # The parser lifts llm.model / llm.reasoning_effort into a bare executor
    # when the executor keys are absent, so a removed value must go from both.
    llm = data.get("llm")
    if isinstance(llm, dict):
        llm = dict(llm)
        llm.pop("reasoning_effort", None)
        model = member.get("model")
        if not llm.keys() - {"model"}:
            llm.pop("model", None)
            _set_or_remove(data, "llm", llm or None)
        elif model is None:
            # The parser rejects a legacy block without llm.model, and keeping
            # the old model would re-lift it into the executor.
            raise OmnigentError(
                f"member '{member['name']}': legacy llm block requires a model",
                code=ErrorCode.INVALID_INPUT,
            )
        else:
            llm["model"] = model
            data["llm"] = llm


def _new_sub_agent_config(member: dict[str, Any]) -> bytes:
    """A minimal sub-agent config in the built-in Polly sub-agent shape."""
    data: dict[str, Any] = {
        "spec_version": 1,
        "name": member["name"],
        "executor": {"type": "omnigent"},
    }
    _apply_member(data, member)
    return _dump_mapping(data)


def _rewrite_sub_agents(
    root: Path,
    subs: list[dict[str, Any]],
    replacements: dict[str, bytes],
    dropped: list[str],
) -> None:
    """Update, create, or drop each role's directory by its config YAML name."""
    by_role = {member["name"]: member for member in subs}
    handled: set[str] = set()
    used: set[str] = set()
    agents = root / "agents"
    for config in sorted(agents.glob("*/config.yaml")) if agents.is_dir() else []:
        data = _load_mapping(config.read_text(encoding="utf-8"))
        name = data.get("name")
        directory = config.parent.relative_to(root).as_posix()
        if not isinstance(name, str) or name not in by_role or name in handled:
            # A directory whose YAML name is not on the roster loses all entries.
            dropped.append(directory)
            continue
        handled.add(name)
        used.add(directory)
        _apply_member(data, by_role[name])
        replacements[f"{directory}/config.yaml"] = _dump_mapping(data)
    for name, member in by_role.items():
        if name in handled:
            continue
        # A new role takes the first suffix no retained directory holds, so a
        # directory named after the role never clobbers a retained member.
        directory = f"agents/{name}"
        suffix = 2
        while directory in used:
            directory = f"agents/{name}-{suffix}"
            suffix += 1
        used.add(directory)
        replacements[f"{directory}/config.yaml"] = _new_sub_agent_config(member)


def _rewrite_members(
    root: Path,
    config: Path,
    raw: str,
    members: list[dict[str, Any]],
    fields: dict[str, Any],
    replacements: dict[str, bytes],
    dropped: list[str],
) -> None:
    """Fold the member roster into the root config and each role's config."""
    leads = [member for member in members if member.get("lead")]
    if len(leads) != 1:
        raise OmnigentError("members must contain exactly one lead", code=ErrorCode.INVALID_INPUT)
    lead = leads[0]
    subs = [member for member in members if not member.get("lead")]

    data = _load_mapping(raw)
    _apply_member(data, lead)
    for key in ("name", "description"):
        if key in fields:
            _set_or_remove(data, key, fields[key])
    if subs:
        _mutable_mapping(data, "tools")["agents"] = [member["name"] for member in subs]
        data["spawn"] = True
    else:
        tools = data.get("tools")
        if isinstance(tools, dict):
            tools = dict(tools)
            tools.pop("agents", None)
            data["tools"] = tools
    if "instructions" in fields:
        # Use a generated file so inline text matching a bundle filename is
        # never loaded as that file. Reuse our prior path to avoid growing
        # the archive on every edit.
        path = _managed_instructions_path(raw) or f"catalog-instructions-{uuid.uuid4().hex}.md"
        replacements[path] = (fields["instructions"] or "").encode("utf-8")
        data["instructions"] = path
    replacements[config.name] = _dump_mapping(data)
    _rewrite_sub_agents(root, subs, replacements, dropped)


def patch_bundle(bundle: bytes, changes: dict[str, Any]) -> bytes:
    """Patch editable YAML fields of an Agent archive.

    Without ``members`` the change is scalar-only: values are edited in place
    and every unrelated byte is retained. A ``members`` change instead parses
    the root config and each touched ``agents/<role>/config.yaml`` with the
    spec parser's loader and re-dumps them, so comments in those files are
    lost; roles dropped from the roster lose their archive entries. Every
    other archive member is copied unchanged either way. A ``members`` change
    on a single-file Agent (no ``config.yaml``) is rejected: a flat Agent has
    no ``agents/<role>/`` tree to persist the roster in.
    """
    with tempfile.TemporaryDirectory() as temp:
        root = extract_safe(bundle, Path(temp) / "bundle")
        config = root / "config.yaml"
        if not config.is_file():
            candidates = [*root.glob("*.yaml"), *root.glob("*.yml")]
            if len(candidates) != 1:
                raise OmnigentError(
                    "Agent bundle has no unambiguous root configuration",
                    code=ErrorCode.INVALID_INPUT,
                )
            config = candidates[0]
        raw = config.read_text(encoding="utf-8")
        replacements: dict[str, bytes] = {}
        dropped: list[str] = []
        fields = dict(changes)
        members = fields.pop("members", None)
        if members is None:
            if "instructions" in fields:
                # Use a generated file so inline text matching a bundle filename is
                # never loaded as that file. Reuse our prior path to avoid growing
                # the archive on every edit.
                path = (
                    _managed_instructions_path(raw)
                    or f"catalog-instructions-{uuid.uuid4().hex}.md"
                )
                replacements[path] = (fields["instructions"] or "").encode("utf-8")
                fields["instructions"] = path
            replacements[config.name] = _patch_yaml_fields(raw, fields).encode("utf-8")
        else:
            if config.name != "config.yaml":
                raise OmnigentError(
                    "Members can only be edited on a directory Agent bundle "
                    "(config.yaml); this Agent is a single YAML file",
                    code=ErrorCode.INVALID_INPUT,
                )
            _rewrite_members(root, config, raw, members, fields, replacements, dropped)

    output = io.BytesIO()
    seen: set[str] = set()
    with (
        tarfile.open(fileobj=io.BytesIO(bundle), mode="r:*") as source,
        tarfile.open(fileobj=output, mode="w:gz") as target,
    ):
        for member in source:
            normalized = str(PurePosixPath(member.name))
            if normalized in seen:
                raise OmnigentError(
                    "Duplicate archive member; upload a normalized bundle to edit",
                    code=ErrorCode.INVALID_INPUT,
                )
            seen.add(normalized)
            if any(normalized == path or normalized.startswith(f"{path}/") for path in dropped):
                continue
            if normalized in replacements and member.isfile():
                data = replacements.pop(normalized)
                info = copy.copy(member)
                info.pax_headers = dict(info.pax_headers)
                info.pax_headers.pop("size", None)
                info.size = len(data)
                target.addfile(info, io.BytesIO(data))
            else:
                target.addfile(member, source.extractfile(member) if member.isfile() else None)
        for path, data in replacements.items():
            info = tarfile.TarInfo(path)
            info.size = len(data)
            info.mode = 0o600
            target.addfile(info, io.BytesIO(data))
    return output.getvalue()
