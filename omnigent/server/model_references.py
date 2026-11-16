"""Owner-scoped inventory and guarded replacement of saved model references."""

from __future__ import annotations

import asyncio
import io
import tarfile
from collections.abc import Callable, Iterator
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Literal

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.models.model_reference import (
    ALIASES_NAMESPACE,
    alias_pairs,
    canonical_pool,
    equivalent_ids,
    provider_groups,
    split_model_id,
)
from omnigent.server.auth import local_single_user_enabled
from omnigent.server.model_resolution import resolve_catalog_model, usable_catalog
from omnigent.server.session_choice import (
    ChoiceRevisionConflict,
    choice_revision,
    parse_session_choice,
)
from omnigent.stores.project_store import project_config_revision

Path = tuple[str | int, ...]


class ReferenceConflict(ValueError):
    """An observed reference changed, or two limit keys would collide."""


@dataclass(frozen=True)
class ModelReference:
    kind: str
    entity_id: str
    label: str
    path: Path
    model: str
    harness: str | None
    hosts: frozenset[str] | None = None
    manual: bool = False
    pool: str | None = None

    def payload(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "entity_id": self.entity_id,
            "label": self.label,
            "path": "/"
            + "/".join(str(part).replace("~", "~0").replace("/", "~1") for part in self.path),
            "saved": self.model,
            "harness": self.harness,
            "manual": self.manual,
        }


@dataclass
class ReferenceEntity:
    kind: str
    id: str
    label: str
    document: Any
    references: list[ModelReference] = field(default_factory=list)
    write: Callable[[Callable[[Any], Any]], None] | None = None

    def add(
        self,
        path: Path,
        model: Any,
        harness: str | None,
        *,
        hosts=None,
        kind=None,
        manual=False,
        pool=None,
    ):
        if isinstance(model, str) and model:
            self.references.append(
                ModelReference(
                    kind or self.kind,
                    self.id,
                    self.label,
                    path,
                    model,
                    harness,
                    hosts,
                    manual or self.write is None,
                    pool,
                )
            )


def _get(document: Any, path: Path) -> Any:
    for part in path:
        document = document[part]
    return document


def _walk_rows(
    entity: ReferenceEntity, value: Any, path: Path, harness_for: Callable, *, hosts=None
):
    if isinstance(value, dict):
        if isinstance(value.get("host"), str):
            hosts = frozenset({value["host"]})
        if "model" in value:
            entity.add(
                (*path, "model"), value["model"], harness_for(value.get("agent")), hosts=hosts
            )
        for key, child in value.items():
            if isinstance(child, (dict, list)):
                _walk_rows(entity, child, (*path, key), harness_for, hosts=hosts)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _walk_rows(entity, child, (*path, index), harness_for, hosts=hosts)


def _master_refs(entity: ReferenceEntity):
    for host, harnesses in (entity.document or {}).items():
        if isinstance(harnesses, dict):
            for harness, entry in harnesses.items():
                if isinstance(entry, dict):
                    entity.add(
                        (host, harness, "model"),
                        entry.get("model"),
                        None if harness == "*" else harness,
                        hosts=frozenset({host}) if host != "*" else None,
                    )


def _manual_spec_references(entity: ReferenceEntity, spec: Any, path: Path = ()) -> None:
    from omnigent.spec.types import FunctionPolicySpec, ProviderAuth

    auth = spec.executor.auth
    if isinstance(auth, ProviderAuth):
        entity.add(
            (*path, "executor", "auth", "name"), auth.name, None, kind="manual_auth", manual=True
        )
    for policy_index, policy in enumerate(
        (spec.guardrails.policies if spec.guardrails else None) or []
    ):
        if isinstance(policy, FunctionPolicySpec) and policy.function:
            models = (policy.function.arguments or {}).get("expensive_models", [])
            for index, model in enumerate(models if isinstance(models, list) else []):
                entity.add(
                    (
                        *path,
                        "guardrails",
                        "policies",
                        policy_index,
                        "function",
                        "arguments",
                        "expensive_models",
                        index,
                    ),
                    model,
                    None,
                    kind="manual_cost_policy",
                    manual=True,
                )
    for index, child in enumerate(spec.sub_agents):
        _manual_spec_references(entity, child, (*path, "sub_agents", index))


def _manual_bundle_references(entity: ReferenceEntity, bundle: bytes, spec: Any) -> None:
    from omnigent.server.custom_agent_bundles import _load_mapping

    _manual_spec_references(entity, spec)
    # The spec parser consolidates deprecated llm fields into executor, so inspect
    # stored YAML to keep those separate connection-bound settings visible.
    with tarfile.open(fileobj=io.BytesIO(bundle), mode="r:*") as archive:
        for member in archive.getmembers():
            if not member.isfile() or member.name.rsplit("/", 1)[-1] != "config.yaml":
                continue
            stream = archive.extractfile(member)
            if stream is None:
                continue
            raw = _load_mapping(stream.read().decode("utf-8"))
            llm = raw.get("llm")
            if not isinstance(llm, dict):
                continue
            path = ("bundle", member.name, "llm")
            entity.add((*path, "model"), llm.get("model"), None, kind="manual_llm", manual=True)
            fallbacks = llm.get("fallback_models", [])
            for index, model in enumerate(fallbacks if isinstance(fallbacks, list) else []):
                entity.add(
                    (*path, "fallback_models", index), model, None, kind="manual_llm", manual=True
                )


def _replace_bundle_models(bundle: bytes, models: dict[str, str | None]) -> bytes:
    from copy import copy

    from omnigent.server.custom_agent_bundles import _load_mapping, _patch_yaml_fields

    output = io.BytesIO()
    found = set()
    with (
        tarfile.open(fileobj=io.BytesIO(bundle), mode="r:*") as source,
        tarfile.open(fileobj=output, mode="w:gz") as target,
    ):
        for member in source:
            stream = source.extractfile(member) if member.isfile() else None
            if stream is None or member.name.rsplit("/", 1)[-1] != "config.yaml":
                target.addfile(member, stream)
                continue
            data = stream.read()
            raw = _load_mapping(data.decode("utf-8"))
            name = raw.get("name")
            if name in models:
                if name in found:
                    raise ReferenceConflict("Agent member names are ambiguous")
                found.add(name)
                executor = dict(raw.get("executor") or {})
                executor["model"] = models[name]
                data = _patch_yaml_fields(data.decode("utf-8"), {"executor": executor}).encode()
            info = copy(member)
            info.pax_headers = {
                key: value for key, value in info.pax_headers.items() if key != "size"
            }
            info.size = len(data)
            target.addfile(info, io.BytesIO(data))
    if models.keys() - found:
        raise ReferenceConflict("Agent members changed; reload before replacing models")
    return output.getvalue()


def _replace_document(
    document: Any,
    original: Any,
    references: list[ModelReference],
    replacement: Callable[[str], str],
) -> Any:
    updated = deepcopy(document)
    for ref in references:
        try:
            current = _get(updated, ref.path)
            expected = _get(original, ref.path)
        except (KeyError, IndexError, TypeError):
            raise ReferenceConflict("A model reference changed; reload and retry") from None
        if current != expected:
            raise ReferenceConflict("A model reference changed; reload and retry")
        parent = _get(updated, ref.path[:-1])
        if ref.pool is not None:
            product = ref.pool.partition(":")[0]
            key = f"{product}:{replacement(ref.model)}"
            if key != ref.pool:
                if key in parent:
                    raise ReferenceConflict("Replacement would merge two limit keys")
                parent[key] = parent.pop(ref.pool)
        else:
            parent[ref.path[-1]] = replacement(ref.model)
    return updated


def reference_entities(
    app: Any, owner: str, *, user_id: str | None, create_if_missing: bool = True
) -> list[ReferenceEntity]:
    state = app.state
    entities: list[ReferenceEntity] = []
    prefs = getattr(state, "user_preferences_store", None)
    settings = (prefs.get(owner) or {}).get("settings", {}) if prefs else {}
    harnesses: dict[str, str | None] = {}

    def harness_for(agent_id):
        if not isinstance(agent_id, str) or not agent_id:
            return None
        if agent_id not in harnesses:
            from omnigent.entities import Conversation
            from omnigent.server.routes._sessions.helpers import _resolve_harness_impl

            agents = getattr(state, "agent_store", None)
            agent = agents.get(agent_id) if agents is not None else None
            harnesses[agent_id] = (
                _resolve_harness_impl(
                    Conversation(
                        id="",
                        root_conversation_id="",
                        created_at=0,
                        updated_at=0,
                        agent_id=agent_id,
                    ),
                    agent_store=agents,
                    agent_cache=getattr(state, "agent_cache", None),
                    agent=agent,
                )
                if agent
                else None
            )
        return harnesses[agent_id]

    if prefs:
        snapshot = prefs.session_choice(owner)
        if snapshot.get("configurationError"):
            raise OmnigentError(
                "Repair session choice settings before checking model references",
                code=ErrorCode.CONFLICT,
            )
        document = snapshot["settings"]
        if document is not None:
            # Parse first so a newer/unknown document is never downgraded by a replacement.
            parse_session_choice(document)

            def write_choice(transform):
                current = prefs.session_choice(owner)
                try:
                    prefs.replace_session_choice(
                        owner,
                        transform(current["settings"]),
                        current["revision"],
                        create_if_missing=create_if_missing,
                    )
                except ChoiceRevisionConflict:
                    current = prefs.session_choice(owner)
                    prefs.replace_session_choice(
                        owner,
                        transform(current["settings"]),
                        current["revision"],
                        create_if_missing=create_if_missing,
                    )

            entity = ReferenceEntity(
                "session_choice", "session_choice", "Session choice", document, write=write_choice
            )
            _walk_rows(entity, document.get("presets", []), ("presets",), harness_for)
            for host, tiers in document.get("hosts", {}).items():
                _walk_rows(entity, tiers, ("hosts", host), harness_for, hosts=frozenset({host}))
            _walk_rows(entity, document.get("limitRules", []), ("limitRules",), harness_for)
            for pool in document.get("limits", {}):
                product, separator, model = pool.partition(":")
                if separator and model:
                    entity.add(
                        ("limits", pool), model, product, kind="session_choice_limit", pool=pool
                    )
            entities.append(entity)

        master = ReferenceEntity(
            "calling_default",
            "calling_defaults",
            "Calling defaults",
            settings.get("calling_defaults", {}),
            write=lambda transform: prefs.transform_namespace(
                owner, "calling_defaults", transform, create_if_missing=create_if_missing
            ),
        )
        _master_refs(master)
        entities.append(master)

        collab = settings.get("session_collab", {})
        periods = ReferenceEntity(
            "quota_period",
            "quota_period",
            "Quota display exceptions",
            collab,
            write=lambda transform: prefs.transform_namespace(
                owner, "session_collab", transform, create_if_missing=create_if_missing
            ),
        )
        _walk_rows(periods, collab.get("quotaPeriodRules", []), ("quotaPeriodRules",), harness_for)
        entities.append(periods)

    projects = getattr(state, "project_store", None)
    if projects:
        for project in projects.list(user_id=user_id, include_archived=True):

            def write_project(transform, project=project):
                result = projects.transform_config(
                    project.id,
                    user_id=user_id,
                    expected_revision=project_config_revision(project.config),
                    transform=transform,
                )
                if result is None:
                    raise ReferenceConflict("Project was removed")

            entity = ReferenceEntity(
                "project_calling_default",
                project.id,
                project.name,
                project.config,
                write=write_project,
            )
            entity.add(
                ("model",),
                project.config.get("model"),
                harness_for(project.config.get("agent_id")),
                kind="project_legacy_model",
            )
            for host, entry in project.config.get("calling_defaults", {}).items():
                for harness, value in entry.get("harnesses", {}).items():
                    entity.add(
                        ("calling_defaults", host, "harnesses", harness, "model"),
                        value.get("model"),
                        harness,
                        hosts=frozenset({host}),
                    )
            entities.append(entity)

    tasks = getattr(state, "scheduled_task_store", None)
    if tasks:
        for task in tasks.list(owner_user_id=user_id):
            if task.user_id != user_id:
                continue

            def write_task(transform, task=task):
                updated = transform({"model": task.model_override})
                if not tasks.replace_model_if_matches(
                    task.id,
                    user_id=user_id,
                    expected_model=task.model_override,
                    model=updated["model"],
                ):
                    raise ReferenceConflict("Scheduled task model changed")

            entity = ReferenceEntity(
                "scheduled_task",
                task.id,
                task.name,
                {"model": task.model_override},
                write=write_task,
            )
            entity.add(
                ("model",),
                task.model_override,
                harness_for(task.agent_id),
                hosts=frozenset({task.host_id}) if task.host_id else None,
            )
            entities.append(entity)

    custom = getattr(state, "custom_agents_store", None)
    artifacts = getattr(state, "artifact_store", None)
    if custom and artifacts:
        from omnigent.server.bundles import bundle_location, validate_agent_bundle

        offset = 0
        while rows := custom.list(owner, 100, offset):
            for row in rows:

                def write_custom(transform, row=row):
                    updated = transform(
                        {"model": row.get("model"), "members": row.get("members") or []}
                    )
                    bundle = artifacts.get(row["bundle_location"])
                    saved = validate_agent_bundle(
                        bundle, enforce_handler_allowlist=not local_single_user_enabled()
                    )
                    if not saved.name:
                        raise ReferenceConflict(
                            "Agent has no member name; edit its model manually"
                        )
                    models = {saved.name: updated["model"]}
                    models.update(
                        {member["name"]: member["model"] for member in updated["members"]}
                    )
                    data = _replace_bundle_models(bundle, models)
                    spec = validate_agent_bundle(
                        data, enforce_handler_allowlist=not local_single_user_enabled()
                    )
                    if spec.executor.model != updated["model"]:
                        raise ReferenceConflict(
                            "Lead and member model settings differ; edit the Agent first"
                        )
                    location = bundle_location(row["id"], data)
                    artifacts.put(location, data)
                    custom.update(
                        owner, row["id"], row["version"], {**updated, "bundle_location": location}
                    )

                entity = ReferenceEntity(
                    "custom_agent",
                    row["id"],
                    row["name"],
                    {"model": row.get("model"), "members": row.get("members") or []},
                    write=write_custom,
                )
                entity.add(("model",), row.get("model"), row.get("harness"))
                for index, member in enumerate(row.get("members") or []):
                    entity.add(
                        ("members", index, "model"),
                        member.get("model"),
                        member.get("harness"),
                        kind="custom_agent_member",
                        hosts=frozenset({member["host_id"]}) if member.get("host_id") else None,
                    )
                bundle = artifacts.get(row["bundle_location"])
                spec = validate_agent_bundle(
                    bundle,
                    enforce_handler_allowlist=not local_single_user_enabled(),
                )
                _manual_bundle_references(entity, bundle, spec)
                entities.append(entity)
            offset += len(rows)

    agents = getattr(state, "agent_store", None)
    if agents and artifacts:
        from omnigent.server.agent_config import edit_agent_bundle
        from omnigent.server.bundles import bundle_location, validate_agent_bundle
        from omnigent.server.schemas import AgentConfigPatch

        after = None
        while True:
            page = agents.list_user_agents(user_id, limit=100, after=after)
            for agent in page.data:
                bundle = artifacts.get(agent.bundle_location)
                spec = validate_agent_bundle(
                    bundle, enforce_handler_allowlist=not local_single_user_enabled()
                )

                def write_bundle(transform, agent=agent, bundle=bundle):
                    saved = validate_agent_bundle(
                        bundle, enforce_handler_allowlist=not local_single_user_enabled()
                    )
                    updated = transform({"model": saved.executor.model})
                    data, _ = edit_agent_bundle(bundle, AgentConfigPatch(model=updated["model"]))
                    location = bundle_location(agent.id, data)
                    artifacts.put(location, data)
                    if (
                        agents.update_configuration(agent, location, description=agent.description)
                        is None
                    ):
                        raise ReferenceConflict("Agent configuration changed")

                entity = ReferenceEntity(
                    "agent_bundle",
                    agent.id,
                    agent.name,
                    {"model": spec.executor.model},
                    write=write_bundle
                    if agents.supports_config_edit and spec.executor.type == "omnigent"
                    else None,
                )
                entity.add(("model",), spec.executor.model, spec.executor.harness_kind)
                _manual_bundle_references(entity, bundle, spec)
                entities.append(entity)
            if not page.has_more or not page.last_id:
                break
            after = page.last_id
    return entities


def iter_model_references(
    app: Any, owner: str, *, user_id: str | None
) -> Iterator[ModelReference]:
    for entity in reference_entities(app, owner, user_id=user_id):
        yield from entity.references


def _catalogs(app: Any, owner: str) -> tuple[list[dict[str, Any]], list[Any]]:
    hosts = (
        app.state.host_store.list_hosts(owner) if getattr(app.state, "host_store", None) else []
    )
    registry = getattr(app.state, "host_registry", None)
    hosts = [
        host for host in hosts if registry is not None and registry.get(host.host_id) is not None
    ]
    store = getattr(app.state, "host_model_catalog_cache_store", None)
    records = store.list([host.host_id for host in hosts]) if store and hosts else []
    return [record for record in records if usable_catalog(record) is not None], hosts


def _aliases(app: Any, owner: str) -> dict[str, Any]:
    store = getattr(app.state, "user_preferences_store", None)
    return (
        (store.get(owner) or {}).get("settings", {}).get(ALIASES_NAMESPACE, {"pairs": []})
        if store
        else {"pairs": []}
    )


def _applies(ref: ModelReference, record: dict[str, Any]) -> bool:
    return (ref.hosts is None or record["host_id"] in ref.hosts) and (
        ref.harness is None
        or record["harness"] == ref.harness
        or record["harness"].removesuffix("-native") == ref.harness
    )


def check_model_references_sync(app: Any, owner: str, *, user_id: str | None) -> dict[str, Any]:
    aliases = _aliases(app, owner)
    groups = provider_groups(alias_pairs(aliases))
    records, hosts = _catalogs(app, owner)
    rows = []
    entities = reference_entities(app, owner, user_id=user_id)
    for entity in entities:
        for ref in entity.references:
            if ref.manual:
                if split_model_id(ref.model)[0] in groups or (
                    ref.kind in {"manual_auth", "manual_cost_policy"}
                    and any(name in ref.model for name in groups)
                ):
                    rows.append(
                        {
                            **ref.payload(),
                            "host": None,
                            "outcome": "manual",
                            "resolved": None,
                            "candidates": [],
                            "still_listed_on": [],
                        }
                    )
                continue
            still_listed = sorted(
                {
                    record["host_id"]
                    for record in records
                    if _applies(ref, record)
                    and any(
                        model in (usable_catalog(record) or ())
                        for model in equivalent_ids(ref.model, groups)
                    )
                }
            )
            for record in records:
                if not _applies(ref, record):
                    continue
                result = resolve_catalog_model(
                    ref.model, usable_catalog(record), groups, record["harness"]
                )
                if result.outcome not in {"substituted", "missing"}:
                    continue
                rows.append(
                    {
                        **ref.payload(),
                        "host": record["host_id"],
                        "harness": record["harness"],
                        "outcome": result.outcome,
                        "resolved": result.model,
                        "candidates": list(result.candidates),
                        "still_listed_on": still_listed,
                    }
                )
        limits = entity.document.get("limits", {}) if entity.kind == "session_choice" else {}
        seen = {}
        for key, value in limits.items():
            canonical = canonical_pool(key, groups)
            if canonical in seen and limits[seen[canonical]] != value:
                rows.append(
                    {
                        "kind": "limit_collision",
                        "entity_id": entity.id,
                        "label": entity.label,
                        "path": "/limits",
                        "saved": key,
                        "host": None,
                        "outcome": "collision",
                        "resolved": canonical,
                        "candidates": [seen[canonical], key],
                        "manual": True,
                        "still_listed_on": [],
                    }
                )
            seen[canonical] = key
    return {
        "rows": rows,
        "aliases": aliases,
        "aliases_revision": choice_revision(aliases),
        "hosts": [{"id": host.host_id, "name": host.name} for host in hosts],
        "models": sorted(
            {model for record in records for model in (usable_catalog(record) or ())}
        ),
        "host_config_manual": bool(groups),
    }


async def check_model_references(app: Any, owner: str, *, user_id: str | None) -> dict[str, Any]:
    return await asyncio.to_thread(check_model_references_sync, app, owner, user_id=user_id)


def replace_model_references_sync(
    app: Any,
    owner: str,
    *,
    user_id: str | None,
    mode: Literal["model", "provider"],
    source: str,
    target: str,
    create_if_missing: bool = True,
) -> dict[str, Any]:
    if (
        not source
        or not target
        or source == target
        or any(char.isspace() for char in source + target)
    ):
        raise ValueError("Choose distinct, non-empty model or provider names without whitespace")
    aliases = _aliases(app, owner)
    pairs = list(alias_pairs(aliases))
    if mode == "provider":
        alias_pairs(
            {"pairs": [*aliases["pairs"], [source, target]]}
            if (source, target) not in pairs and (target, source) not in pairs
            else aliases
        )

    def matches(value: str) -> bool:
        return value == source if mode == "model" else split_model_id(value)[0] == source

    def replacement(value: str) -> str:
        return target if mode == "model" else f"{target}/{split_model_id(value)[1]}"

    entities = reference_entities(app, owner, user_id=user_id, create_if_missing=create_if_missing)
    affected = [
        (entity, [ref for ref in entity.references if not ref.manual and matches(ref.model)])
        for entity in entities
    ]
    if mode == "model":
        records, _ = _catalogs(app, owner)
        groups = provider_groups(pairs)
        for _, references in affected:
            for ref in references:
                if not any(
                    _applies(ref, record)
                    and resolve_catalog_model(
                        target, usable_catalog(record), groups, record["harness"]
                    ).outcome
                    in {"exact", "pinned"}
                    for record in records
                ):
                    raise ValueError(
                        "The replacement model must be offered by a connected host for every "
                        "affected harness; sync models first"
                    )

    committed, conflicts, failed = [], [], []
    for entity, references in affected:
        if not references:
            continue
        item = {
            "kind": entity.kind,
            "entity_id": entity.id,
            "paths": [ref.payload()["path"] for ref in references],
            "count": len(references),
        }
        try:
            assert entity.write is not None
            entity.write(
                lambda current, entity=entity, references=references: _replace_document(
                    current, entity.document, references, replacement
                )
            )
            committed.append(item)
        except ReferenceConflict as exc:
            conflicts.append({**item, "reason": str(exc)})
        except ChoiceRevisionConflict:
            conflicts.append({**item, "reason": "Session choice changed during replacement"})
        except OmnigentError as exc:
            (conflicts if exc.code == ErrorCode.CONFLICT else failed).append(item)
        except Exception:  # noqa: BLE001 — return partial results without logging bundle data
            failed.append(item)
    alias_status = "not_requested"
    if mode == "provider":
        try:
            store = app.state.user_preferences_store

            def union(current):
                current = current or {"pairs": []}
                current_pairs = alias_pairs(current)
                if (source, target) in current_pairs or (target, source) in current_pairs:
                    return current
                updated = {"pairs": [*current["pairs"], [source, target]]}
                alias_pairs(updated)
                return updated

            store.transform_namespace(
                owner, ALIASES_NAMESPACE, union, create_if_missing=create_if_missing
            )
            alias_status = (
                "present" if (source, target) in pairs or (target, source) in pairs else "added"
            )
        except Exception:  # noqa: BLE001 — earlier entity writes still need an honest receipt
            alias_status = "failed"
    try:
        remaining = sum(
            matches(ref.model) for ref in iter_model_references(app, owner, user_id=user_id)
        )
    except Exception:  # noqa: BLE001 — a failed read must not hide already committed writes
        remaining = None
        failed.append(
            {
                "kind": "inventory",
                "entity_id": "",
                "reason": "Could not recheck remaining references",
            }
        )
    return {
        "committed": committed,
        "conflicts": conflicts,
        "failed": failed,
        "remaining": remaining,
        "aliases": alias_status,
        "complete": not conflicts and not failed and remaining == 0 and alias_status != "failed",
        "count": sum(item["count"] for item in committed),
    }


async def replace_model_references(app: Any, owner: str, **kwargs: Any) -> dict[str, Any]:
    return await asyncio.to_thread(replace_model_references_sync, app, owner, **kwargs)
