"""Session selection settings use one validated, revision-checked writer."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from datetime import datetime
from typing import Any, cast

import httpx
import pytest
from pydantic import ValidationError

from omnigent.entities import Conversation
from omnigent.server.session_choice import (
    CHOICE_LABEL,
    SLOT_LABEL,
    ChoiceRequest,
    ChoiceRevisionConflict,
    ChoiceState,
    FloorOff,
    Link,
    candidate_quota,
    choice_document,
    choice_revision,
    choose,
    floor_off_active,
    holds_slot,
    model_family,
    parse_session_choice,
    plan_choice,
    pool_of_session,
    quota_reset_at,
    resolve_limit_rules,
)
from omnigent.server.user_preferences_store import (
    SqlAlchemyUserPreferencesStore,
    read_collab_settings,
)
from omnigent.usage.contract import UsageAccountView, UsageWindowView
from tests.server.test_user_preferences import _preferences_app

OWNER = "owner@example.test"


def rules_document(rules):
    return parse_session_choice({"schemaVersion": 2, "limitRules": rules})


@pytest.mark.parametrize("model_first", [False, True])
def test_rule_fields_use_specificity_then_visible_order(model_first):
    base = {
        "id": "base",
        "agent": "agent-a",
        "maxRunning": 3,
        "reserve": {"mode": "total", "window": "weekly", "pct": 20, "stopPct": 5},
    }
    host = {
        "id": "host",
        "agent": "agent-a",
        "host": "host-a",
        "maxRunning": 2,
        "reserve": {"pct": 10},
    }
    model = {
        "id": "model",
        "agent": "agent-a",
        "model": "model-x",
        "maxRunning": 4,
        "reserve": {"stopPct": 2},
    }
    rules = rules_document([base, *([model, host] if model_first else [host, model])]).limit_rules
    result = resolve_limit_rules(rules, agent="agent-a", host="host-a", model="model-x")
    assert result["maxRunning"].value == (4 if model_first else 2)
    assert result["maxRunning"].source_rule_id == ("model" if model_first else "host")
    assert result["maxRunning"].scope.model == ("model-x" if model_first else None)
    assert result["maxRunning"].scope.host == (None if model_first else "host-a")
    assert result["reserve.pct"].value == 10
    assert result["reserve.pct"].source_rule_id == "host"
    assert result["reserve.stopPct"].value == 2
    assert result["reserve.stopPct"].source_rule_id == "model"
    assert result["reserve.mode"].value == "total"
    assert result["reserve.window"].source_rule_id == "base"
    assert result["reserve.window"].specificity == 1
    exact = {
        "id": "exact",
        "agent": "agent-a",
        "host": "host-a",
        "model": "model-x",
        "maxRunning": None,
    }
    exact_rules = rules_document([base, host, model, exact]).limit_rules
    unlimited = resolve_limit_rules(exact_rules, agent="agent-a", host="host-a", model="model-x")
    assert unlimited["maxRunning"].payload() == {
        "defined": True,
        "value": None,
        "sourceRuleId": "exact",
        "specificity": 3,
        "scope": {"agent": "agent-a", "host": "host-a", "model": "model-x"},
    }


def test_specific_looser_cap_overrides_broad_and_identity_is_exact():
    rules = rules_document(
        [
            {"id": "base", "agent": "agent-a", "maxRunning": 3},
            {"id": "specific", "agent": "agent-a", "model": "model-x", "maxRunning": 4},
            {"id": "empty", "agent": "agent-a", "host": "host-a", "model": "model-x"},
        ]
    ).limit_rules
    for host in ("host-a", "other-host"):
        result = resolve_limit_rules(rules, agent="agent-a", host=host, model="model-x")
        assert result["maxRunning"].value == 4
        assert result["maxRunning"].scope.host is None
    for model in ("Model-x", "model-x-extra", "provider/model-x", None):
        assert (
            resolve_limit_rules(rules, agent="agent-a", host="host-a", model=model)[
                "maxRunning"
            ].value
            == 3
        )
    missing = resolve_limit_rules(rules, agent="Agent-a", host="host-a", model="model-x")
    assert missing["maxRunning"].payload() == {
        "defined": False,
        "value": None,
        "sourceRuleId": None,
        "specificity": None,
        "scope": None,
    }


def test_patch_roundtrip_preserves_absence_null_zero_empty_and_copies():
    raw = {
        "schemaVersion": 2,
        "workTypes": [{"id": "work", "label": "Work"}],
        "limitRules": [
            {
                "id": "base",
                "agent": "agent-a",
                "maxRunning": 3,
                "reserve": {"mode": "total", "window": "weekly", "pct": 10, "stopPct": 0},
                "reserveExempt": ["work"],
            },
            {
                "id": "specific",
                "agent": "agent-a",
                "host": "host-a",
                "maxRunning": None,
                "reserve": None,
                "reserveExempt": [],
            },
            {"id": "empty", "agent": "agent-a", "model": "model-x", "reserve": {}},
        ],
    }
    settings = parse_session_choice(raw)
    saved = choice_document(settings)
    assert saved["limitRules"][1]["reserve"] == {"pct": None, "stopPct": None}
    assert saved["limitRules"][1]["maxRunning"] is None
    assert saved["limitRules"][1]["reserveExempt"] == []
    assert saved["limitRules"][0]["reserve"]["stopPct"] == 0
    assert saved["limitRules"][2] == raw["limitRules"][2]
    assert choice_document(parse_session_choice(saved)) == saved
    result = resolve_limit_rules(
        settings.limit_rules, agent="agent-a", host="host-a", model="model-x"
    )
    assert result["reserve.pct"].defined and result["reserve.pct"].value is None
    assert result["reserve.mode"].source_rule_id == "base"
    assert result["reserveExempt"].value == ()
    copied = settings.limit_rules[2].model_copy(update={"max_running": None})
    saved_copy = choice_document(settings.model_copy(update={"limit_rules": (copied,)}))
    assert saved_copy["limitRules"][0]["maxRunning"] is None
    assert "schemaVersion" not in choice_document(parse_session_choice({}))
    assert "limitRules" not in choice_document(parse_session_choice({"schemaVersion": 1}))
    assert choice_document(rules_document([]))["limitRules"] == []
    assert choice_revision(saved) == choice_revision(dict(reversed(list(saved.items()))))


@pytest.mark.parametrize(
    "change",
    [{"maxRunning": value} for value in (0, -1, True, 1.5)]
    + [
        {"agent": ""},
        {"host": ""},
        {"model": "*"},
        {"reserveExempt": None},
        {"reserve": {"mode": None}},
        {"reserve": {"window": None}},
        {"reserveExempt": ["missing"]},
    ],
)
def test_invalid_rule_patch_is_rejected(change):
    with pytest.raises(ValidationError):
        rules_document([{"id": "rule", "agent": "agent-a", **change}])


@pytest.mark.parametrize("field", ["pct", "stopPct"])
@pytest.mark.parametrize("value", [-1, 101, float("inf"), float("-inf"), float("nan"), True, "10"])
def test_rule_threshold_validation_names_the_invalid_field(field, value):
    with pytest.raises(ValidationError) as error:
        rules_document(
            [
                {
                    "id": "rule",
                    "agent": "agent-a",
                    "reserve": {"mode": "total", "window": "weekly", field: value},
                }
            ]
        )
    assert error.value.errors()[0]["loc"] == ("limitRules", 0, "reserve", field)


@pytest.mark.parametrize(
    "raw",
    [
        {"limitRules": []},
        {"schemaVersion": 1, "limitRules": []},
        {"schemaVersion": 2},
        {"schemaVersion": 3},
        {"schemaVersion": True},
        {"schemaVersion": 2, "limitRules": None},
        {"schemaVersion": 2, "limitRules": [{"id": "same", "agent": "a"}] * 2},
    ],
)
def test_rule_document_version_and_ids_are_strict(raw):
    with pytest.raises(ValidationError):
        parse_session_choice(raw)


def test_composed_threshold_validation_includes_unlisted_combinations_and_order():
    base = {
        "id": "base",
        "agent": "agent-a",
        "reserve": {"mode": "total", "window": "weekly", "pct": 20, "stopPct": 5},
    }
    host = {"id": "host", "agent": "agent-a", "host": "host-a", "reserve": {"pct": 2}}
    model = {"id": "model", "agent": "agent-a", "model": "model-x", "reserve": {"stopPct": 1}}
    # The listed host/model intersection is valid; the host/other-model cell is not.
    with pytest.raises(ValidationError) as error:
        rules_document([base, host, model])
    assert "reserve.stopPct" in str(error.value)
    assert "host-a" in str(error.value) and "base" in str(error.value)
    cancel = {"id": "cancel", "agent": "agent-a", "host": "host-a", "reserve": None}
    rules_document([cancel, host, base])
    safe = {"id": "safe", "agent": "agent-a", "host": "host-a", "reserve": {"pct": 10}}
    rules_document([safe, host, base])
    with pytest.raises(ValidationError):
        rules_document([host, safe, base])
    with pytest.raises(ValidationError, match=r"reserve\.mode"):
        rules_document([{"id": "orphan", "agent": "agent-a", "reserve": {"pct": 0}}])
    rules_document([{"id": "cancel", "agent": "agent-a", "reserve": None}])


@pytest.mark.parametrize("patch", [None, "old-page", {"sessionChoice": {}, "childCap": 4}])
def test_saved_v2_roundtrip_namespace_preserves_null_and_rule_order(db_uri, patch):
    store = SqlAlchemyUserPreferencesStore(db_uri)
    raw = {
        **document(),
        "schemaVersion": 2,
        "limitRules": [
            {"id": "first", "agent": "agent-a", "maxRunning": None},
            {"id": "second", "agent": "agent-a", "maxRunning": 3},
        ],
    }
    saved = save(store, raw)
    store.patch_namespace(OWNER, "session_collab", patch)
    assert store.session_choice(OWNER)["settings"] == saved["settings"]
    same = save(store, dict(reversed(list(saved["settings"].items()))))
    assert same["revision"] == saved["revision"]
    changed = deepcopy(saved["settings"])
    changed["limitRules"].reverse()
    assert save(store, changed)["revision"] != saved["revision"]
    before = store.session_choice(OWNER)
    with pytest.raises(ChoiceRevisionConflict):
        save(store, raw, saved["revision"])
    assert store.session_choice(OWNER) == before


def test_v2_downgrade_rejected_but_undo_keeps_v2_envelope(db_uri):
    store = SqlAlchemyUserPreferencesStore(db_uri)
    legacy = save(store, document())["settings"]
    converted = save(store, {**legacy, "schemaVersion": 2, "limitRules": []})
    with pytest.raises(ChoiceRevisionConflict, match="version"):
        save(store, legacy, converted["revision"])
    assert store.session_choice(OWNER)["revision"] == converted["revision"]
    undo = save(store, {**legacy, "schemaVersion": 2, "limitRules": []})
    assert undo["settings"]["limits"] == legacy["limits"]
    assert undo["settings"]["schemaVersion"] == 2
    assert save(store, None)["settings"] is None


def inject_stored_choice(store, raw):
    import json

    with store._write_session("inject_choice_fixture") as session:
        store._upsert_row(
            session,
            user_id=OWNER,
            key="settings.session_collab",
            value=json.dumps({"sessionChoice": raw}),
        )


@pytest.mark.parametrize("version,state", [(2, "invalid"), (99, "unsupported")])
def test_bad_stored_choice_remains_visible_and_cannot_downgrade(db_uri, version, state):
    store = SqlAlchemyUserPreferencesStore(db_uri)
    raw = {"schemaVersion": version, "limitRules": [], "enabled": "invalid"}
    inject_stored_choice(store, raw)
    result = store.session_choice(OWNER)
    assert result["settings"] == raw
    assert result["configurationError"]["state"] == state
    parsed = read_collab_settings(store, OWNER)
    assert parsed.session_choice_error["state"] == state
    assert not parsed.session_choice.enabled
    with pytest.raises(ChoiceRevisionConflict):
        save(store, document(), result["revision"])
    assert store.session_choice(OWNER) == result


def test_stop_owner_discovery_includes_scoped_zero_stop_across_workspaces(db_uri):
    from omnigent.db.db_models import workspace_scope

    store = SqlAlchemyUserPreferencesStore(db_uri)
    for workspace, stop in [(0, 0), (17, 5), (18, None)]:
        with workspace_scope(workspace):
            save(
                store,
                {
                    "schemaVersion": 2,
                    "limitRules": [
                        {
                            "id": "stop",
                            "agent": "agent-a",
                            "reserve": {"mode": "total", "window": "weekly", "stopPct": stop},
                        },
                    ],
                },
            )
    assert {workspace for workspace, owner, _ in store.stop_line_owners() if owner == OWNER} == {
        0,
        17,
    }


def conversion_fixture(store):
    from omnigent.server.user_preferences_store import (
        ChoiceConversionProof,
        LegacyChoiceConversionPlan,
    )

    original = document()
    original["limits"]["unmapped"] = {"maxRunning": 2}
    saved = save(store, original)
    target = deepcopy(saved["settings"])
    target.update(
        schemaVersion=2,
        limitRules=[
            {
                "id": "converted",
                "agent": "agent-a",
                "model": "model-x",
                "reserve": {"mode": "daily", "window": "7d", "pct": 5},
            }
        ],
    )
    del target["limits"]["account-a"]
    proof = ChoiceConversionProof(
        source_revision=saved["revision"],
        mapping_fingerprint="mapping-a",
        replaced_paths=(("limits", "account-a"),),
        new_rule_ids=("converted",),
    )
    plan = LegacyChoiceConversionPlan(
        mapping_fingerprint="mapping-a",
        target=parse_session_choice(target),
        replaced_paths=proof.replaced_paths,
        new_rule_ids=proof.new_rule_ids,
    )
    return saved, target, proof, plan


def test_conversion_proof_and_v2_undo_are_one_revision_transaction(db_uri):
    store = SqlAlchemyUserPreferencesStore(db_uri)
    before, target, proof, plan = conversion_fixture(store)
    calls = []

    def fresh(owner, stored):
        calls.append((owner, stored.schema_version))
        return plan

    saved = store.replace_session_choice(
        OWNER, target, before["revision"], conversion_proof=proof, conversion_plan=fresh
    )
    assert calls == [(OWNER, 1)]
    assert saved["settings"]["limits"] == {"unmapped": {"reserveExempt": [], "maxRunning": 2}}
    assert store.session_choice(OWNER)["settings"] == saved["settings"]
    # Undo restores legacy content in v2, keeping an independent later rule.
    later = deepcopy(saved["settings"])
    later["limitRules"].append({"id": "independent", "agent": "agent-b", "maxRunning": None})
    saved_later = save(store, later)
    undo = {**before["settings"], "schemaVersion": 2, "limitRules": [later["limitRules"][-1]]}
    with pytest.raises(ChoiceRevisionConflict):
        store.replace_session_choice(OWNER, undo, saved["revision"])
    restored = store.replace_session_choice(OWNER, undo, saved_later["revision"])
    assert restored["settings"]["limits"] == before["settings"]["limits"]
    assert restored["settings"]["limitRules"] == [later["limitRules"][-1]]


@pytest.mark.parametrize(
    "fault", ["missing-provider", "mapping", "source", "paths", "rules", "unmapped", "target"]
)
def test_conversion_conflict_does_not_half_clear_legacy(db_uri, fault):
    store = SqlAlchemyUserPreferencesStore(db_uri)
    before, target, proof, plan = conversion_fixture(store)

    def provider(owner, stored):
        return plan

    if fault == "missing-provider":
        provider = None
    elif fault == "mapping":
        plan = replace(plan, mapping_fingerprint="changed")
    elif fault == "source":
        proof = proof.model_copy(update={"source_revision": "stale"})
    elif fault == "paths":
        proof = proof.model_copy(update={"replaced_paths": (("limits", "unmapped"),)})
    elif fault == "rules":
        proof = proof.model_copy(update={"new_rule_ids": ("wrong",)})
    elif fault == "unmapped":
        del target["limits"]["unmapped"]
        plan = replace(plan, target=parse_session_choice(target))
    else:
        target["limitRules"][0]["reserve"]["pct"] = 6
    with pytest.raises(ChoiceRevisionConflict):
        store.replace_session_choice(
            OWNER, target, before["revision"], conversion_proof=proof, conversion_plan=provider
        )
    assert store.session_choice(OWNER)["settings"] == before["settings"]
    assert store.session_choice(OWNER)["revision"] == before["revision"]


@pytest.mark.asyncio
async def test_api_version_capabilities_diagnostics_and_conversion_guard(db_uri, tmp_path):
    store = SqlAlchemyUserPreferencesStore(db_uri)
    app = _preferences_app(db_uri, tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"X-Test-User": OWNER},
    ) as client:
        url = "/v1/me/session-choice"
        read = (await client.get(url)).json()
        assert read["capabilities"]["documentVersions"] == [1, 2]
        saved = await client.put(
            url,
            json={
                "settings": {"schemaVersion": 2, "limitRules": []},
                "expected_revision": read["revision"],
            },
        )
        assert saved.status_code == 200, saved.text
        revision = saved.json()["revision"]
        refused = await client.put(
            url, json={"settings": document(), "expected_revision": revision}
        )
        assert refused.status_code == 409
        bad = await client.put(
            url, json={"settings": {"schemaVersion": 99}, "expected_revision": revision}
        )
        assert bad.status_code == 422
        save(store, None)
        before, target, proof, _ = conversion_fixture(store)
        refused = await client.put(
            url,
            json={
                "settings": target,
                "expected_revision": before["revision"],
                "conversion_proof": proof.model_dump(mode="json"),
            },
        )
        assert refused.status_code == 409
        inject_stored_choice(store, {"schemaVersion": 99, "future": "preserve"})
        read = (await client.get(url)).json()
        assert read["configurationError"]["state"] == "unsupported"
        assert read["settings"] == {"schemaVersion": 99, "future": "preserve"}


@pytest.mark.asyncio
async def test_route_passes_proof_only_to_fresh_owner_scoped_server_plan(db_uri):
    from fastapi import FastAPI

    from omnigent.errors import ErrorCode, OmnigentError
    from omnigent.server.child_cap import ChildCapLocks
    from omnigent.server.routes.session_choice import create_session_choice_router
    from tests.server.test_user_preferences import _HeaderAuthProvider

    store = SqlAlchemyUserPreferencesStore(db_uri)
    before, target, proof, plan = conversion_fixture(store)
    calls = []

    def fresh(owner, stored):
        calls.append(owner)
        return plan

    app = FastAPI()
    app.state.child_cap_locks = ChildCapLocks()
    app.include_router(
        create_session_choice_router(store, _HeaderAuthProvider(), conversion_plan=fresh),
        prefix="/v1",
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        body = {
            "settings": target,
            "expected_revision": before["revision"],
            "conversion_proof": proof.model_dump(mode="json"),
        }
        # The standalone router has no main-app exception-to-HTTP handler.
        with pytest.raises(OmnigentError) as error:
            await client.put("/v1/me/session-choice", json=body)
        assert error.value.code == ErrorCode.UNAUTHORIZED
        other = await client.put(
            "/v1/me/session-choice", json=body, headers={"X-Test-User": "other@example.test"}
        )
        assert other.status_code == 409
        assert calls == []
        result = await client.put(
            "/v1/me/session-choice", json=body, headers={"X-Test-User": OWNER}
        )
        assert result.status_code == 200, result.text
        assert calls == [OWNER]
        assert result.json()["settings"]["limitRules"][0]["id"] == "converted"


@pytest.mark.parametrize("location", ["presets", "hosts"])
def test_conversion_replaces_only_the_proved_link_cap(db_uri, location):
    from omnigent.server.user_preferences_store import (
        ChoiceConversionProof,
        LegacyChoiceConversionPlan,
    )

    store = SqlAlchemyUserPreferencesStore(db_uri)
    original = document()
    if location == "presets":
        link = original["presets"][0]["links"][0]
        path = ("presets", 0, "links", 0, "maxRunning")
    else:
        link = original["hosts"]["host-a"]["implementation"][0][0]["links"][0]
        path = ("hosts", "host-a", "implementation", 0, 0, "links", 0, "maxRunning")
    link.pop("meter")
    link["maxRunning"] = 2
    before = save(store, original)
    target = deepcopy(before["settings"])
    target.update(
        schemaVersion=2,
        limitRules=[{"id": "cap", "agent": "agent-a", "model": "model-x", "maxRunning": 2}],
    )
    entry = target
    for segment in path[:-1]:
        entry = entry[segment]
    del entry[path[-1]]
    proof = ChoiceConversionProof(
        source_revision=before["revision"],
        mapping_fingerprint="mapping",
        replaced_paths=(path,),
        new_rule_ids=("cap",),
    )
    plan = LegacyChoiceConversionPlan(
        "mapping", parse_session_choice(target), proof.replaced_paths, proof.new_rule_ids
    )

    def fresh(owner, stored):
        return plan

    saved = store.replace_session_choice(
        OWNER, target, before["revision"], conversion_proof=proof, conversion_plan=fresh
    )
    assert saved["settings"]["limits"] == before["settings"]["limits"]
    assert saved["settings"]["limitRules"][0]["maxRunning"] == 2


def document():
    links = [{"agent": "codex-native", "model": "gpt-example", "meter": "account-a"}]
    return {
        "enabled": True,
        "workTypes": [{"id": "implementation", "label": "Implementation"}],
        "defaultWorkType": "implementation",
        "presets": [{"id": "code", "name": "Code", "links": deepcopy(links)}],
        "hosts": {
            "host-a": {
                "implementation": [[{"preset": "code", "weight": 2, "links": deepcopy(links)}]]
            }
        },
        "limits": {"account-a": {"reserve": {"mode": "daily", "window": "7d", "pct": 5}}},
    }


def save(store, value, revision=None):
    revision = revision or store.session_choice(OWNER)["revision"]
    return store.replace_session_choice(OWNER, value, revision)


def test_saved_row_name_is_a_snapshot_and_survives_preset_rename(db_uri):
    store = SqlAlchemyUserPreferencesStore(db_uri)
    value = document()
    value["hosts"]["host-a"]["implementation"][0][0]["name"] = "Original code"
    initial = save(store, value)
    value["presets"][0]["name"] = "Renamed code"
    saved = save(store, value, initial["revision"])
    row = saved["settings"]["hosts"]["host-a"]["implementation"][0][0]
    assert row["name"] == "Original code"
    assert row["adjusted"] is False


@pytest.mark.parametrize("schema_version", [1, 2])
def test_revision_is_atomic_and_other_preferences_survive(db_uri, schema_version):
    store = SqlAlchemyUserPreferencesStore(db_uri)
    store.patch_namespace(OWNER, "session_collab", {"childCap": 3})
    initial = store.session_choice(OWNER)

    def attempt(value):
        try:
            return save(store, value, initial["revision"])
        except ChoiceRevisionConflict:
            return "conflict"

    value = document()
    if schema_version == 2:
        value.update(schemaVersion=2, limitRules=[])
    other = deepcopy(value)
    other["strategy"] = "balance"
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, [value, other]))
    assert results.count("conflict") == 1
    stored = store.session_choice(OWNER)
    assert stored["revision"] != initial["revision"]
    assert store.get(OWNER)["settings"]["session_collab"]["childCap"] == 3
    assert read_collab_settings(store, OWNER).session_choice.enabled
    assert store.session_choice("another@example.test")["settings"] is None


@pytest.mark.parametrize("patch", [None, {"sessionChoice": {"enabled": False}}, "old-page"])
def test_legacy_patch_cannot_replace_or_clear_document(db_uri, patch):
    store = SqlAlchemyUserPreferencesStore(db_uri)
    saved = save(store, document())
    store.patch_namespace(OWNER, "session_collab", patch)
    assert store.session_choice(OWNER) == {key: saved[key] for key in ("settings", "revision")}
    assert save(store, None)["settings"] is None


def test_initial_migration_is_not_a_choice_writer(db_uri):
    store = SqlAlchemyUserPreferencesStore(db_uri)
    store.initialize(
        OWNER,
        {
            "version": 1,
            "settings": {"session_collab": {"sessionChoice": document(), "childCap": 2}},
        },
    )
    assert store.session_choice(OWNER)["settings"] is None
    assert read_collab_settings(store, OWNER).child_cap == 2


def test_normalizes_only_link_edits_and_keeps_unapplied_copies(db_uri):
    store = SqlAlchemyUserPreferencesStore(db_uri)
    saved = save(store, document())
    changed = deepcopy(saved["settings"])
    row = changed["hosts"]["host-a"]["implementation"][0][0]
    row["weight"] = 7
    changed["presets"][0]["links"][0]["meter"] = "account-b"
    saved = save(store, changed)
    assert saved["corrected"] == []
    assert not saved["settings"]["hosts"]["host-a"]["implementation"][0][0]["adjusted"]
    changed = deepcopy(saved["settings"])
    row = changed["hosts"]["host-a"]["implementation"][0][0]
    row["links"][0]["model"] = "other-model"
    saved = save(store, changed)
    assert saved["corrected"] == ["hosts.host-a.implementation.0.0"]
    row = saved["settings"]["hosts"]["host-a"]["implementation"][0][0]
    assert row["adjusted"]
    row["adjusted"] = False
    saved = save(store, saved["settings"])
    assert saved["corrected"] == ["hosts.host-a.implementation.0.0"]
    changed = deepcopy(saved["settings"])
    row = changed["hosts"]["host-a"]["implementation"][0][0]
    row["links"] = deepcopy(changed["presets"][0]["links"])
    row["adjusted"] = False
    saved = save(store, changed)
    assert saved["corrected"] == []
    assert saved["settings"]["hosts"]["host-a"]["implementation"][0][0]["weight"] == 7
    changed = deepcopy(saved["settings"])
    changed["presets"] = []
    assert save(store, changed)["corrected"] == ["hosts.host-a.implementation.0.0"]


@pytest.mark.parametrize(
    "change",
    [
        {"enabled": 1},
        {"unknown": True},
        {"defaultWorkType": "missing"},
        {"workTypes": [{"id": "Bad id", "label": "Bad id"}]},
        {"workTypes": [{"id": "implementation", "label": "I", "definition": "x" * 161}]},
        {"presets": [{"id": "empty", "name": "Empty", "links": []}]},
    ],
)
def test_strict_document_and_fail_safe_runtime(db_uri, change):
    bad = {**document(), **change}
    with pytest.raises(ValidationError):
        parse_session_choice(bad)
    store = SqlAlchemyUserPreferencesStore(db_uri)
    # Persisted older/corrupt data may be read; it must turn selection off.
    from omnigent.server.user_preferences_store import _parse_collab_settings

    assert not _parse_collab_settings({"sessionChoice": bad}).session_choice.enabled
    assert not read_collab_settings(store, OWNER).session_choice.enabled


@pytest.mark.asyncio
async def test_api_registered_owner_scoped_revision_and_field_errors(db_uri, tmp_path):
    app = _preferences_app(db_uri, tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        headers = {"X-Test-User": OWNER}
        url = "/v1/me/session-choice"
        initial = (await client.get(url, headers=headers)).json()
        body = {"settings": document(), "expected_revision": initial["revision"]}
        result = await client.put(url, headers=headers, json=body)
        assert result.status_code == 200, result.text
        assert result.json()["corrected"] == []
        assert (await client.put(url, headers=headers, json=body)).status_code == 409
        bad = deepcopy(result.json()["settings"])
        bad["hosts"]["host-a"]["implementation"][0][0]["weight"] = 12
        failed = await client.put(
            url,
            headers=headers,
            json={"settings": bad, "expected_revision": result.json()["revision"]},
        )
        assert failed.status_code == 422
        assert "weight" in failed.text
        assert (await client.get(url, headers={"X-Test-User": "other@example.test"})).json()[
            "settings"
        ] is None
        assert (await client.get(url)).status_code == 401


@pytest.mark.asyncio
async def test_fallback_mode_round_trips_without_changing_return_copies(db_uri, tmp_path):
    app = _preferences_app(db_uri, tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        url = "/v1/me/session-choice"
        headers = {"X-Test-User": OWNER}
        value: dict[str, Any] = document()
        preset = value["presets"][0]["links"]
        row = value["hosts"]["host-a"]["implementation"][0][0]["links"]
        for links in (preset, row):
            links.append({"agent": "alternate", "model": "example-fallback"})
        initial = (await client.get(url, headers=headers)).json()

        async def put(revision):
            return await client.put(
                url, headers=headers, json={"settings": value, "expected_revision": revision}
            )

        base = (await put(initial["revision"])).json()
        for return_mode in ("return", None):
            for links in (preset, row):
                links[1]["fallbackMode"] = return_mode
            returned = await put(base["revision"])
            assert returned.status_code == 200, returned.text
            assert returned.json() == base
            saved = (await client.get(url, headers=headers)).json()
            assert saved["revision"] == base["revision"]
            assert saved["settings"] == base["settings"]
        for links in (preset, row):
            links[1]["fallbackMode"] = "drain"
        drained = await put(base["revision"])
        assert drained.status_code == 200, drained.text
        saved = (await client.get(url, headers=headers)).json()
        assert saved["revision"] != base["revision"]
        assert saved["settings"]["presets"][0]["links"][1]["fallbackMode"] == "drain"
        assert saved["settings"]["hosts"]["host-a"]["implementation"][0][0]["adjusted"] is False
        assert (
            saved["settings"]["hosts"]["host-a"]["implementation"][0][0]["links"][1][
                "fallbackMode"
            ]
            == "drain"
        )
        assert saved["runtime"] == {"settingsRevision": saved["revision"], "drainLocks": []}
        row[1]["fallbackMode"] = "sideways"
        assert (await put(saved["revision"])).status_code == 422


@pytest.mark.asyncio
async def test_floor_controls_and_preview_use_owner_tiles_without_changing_document(
    db_uri, tmp_path
):
    import time

    from omnigent.stores.host_store import HostStore
    from omnigent.usage.contract import UsageReport

    hosts = HostStore(db_uri)
    host = "1" * 32
    hosts.upsert_on_connect(host, "sample-host", OWNER)
    app = _preferences_app(db_uri, tmp_path, host_store=hosts)
    agent_id = "a" * 32
    app.state.agent_store.create(agent_id, "code", "test:///bundle")
    settings = document()
    settings["hosts"] = {host: settings["hosts"]["host-a"]}
    settings["presets"][0]["links"][0]["agent"] = agent_id
    settings["hosts"][host]["implementation"][0][0]["links"][0]["agent"] = agent_id
    now = time.time()
    app.state.agent_quota_service.report_account(
        OWNER,
        "account-a",
        UsageReport.model_validate(
            {
                "observed_at": now,
                "windows": [
                    {
                        "id": "7d",
                        "percent_used": 90,
                        "window_minutes": 10080,
                        "resets_at": now + 4 * 86400,
                    }
                ],
            }
        ),
        now=now,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"X-Test-User": OWNER},
    ) as client:
        url = "/v1/me/session-choice"
        revision = (await client.get(url)).json()["revision"]
        saved = await client.put(url, json={"settings": settings, "expected_revision": revision})
        assert saved.status_code == 200, saved.text
        revision = saved.json()["revision"]
        preview = await client.post(url + "/preview", json={"host_id": host})
        assert preview.status_code == 200 and preview.json()["outcome"] == "hold", preview.text
        query_preview = await client.get(url + "/preview", params={"host_id": host})
        assert query_preview.status_code == 200, query_preview.text
        assert query_preview.json() == preview.json()
        floor = await client.post(url + "/floor", json={"tile": "account-a", "off": True})
        assert floor.status_code == 200, floor.text
        assert floor.json()["account-a"]["until"] == "reset"
        assert (await client.get(url)).json()["revision"] == revision
        for _ in range(2):
            preview = await client.post(url + "/preview", json={"host_id": host})
            assert preview.json()["outcome"] == "pick", preview.text
        from omnigent.server.session_choice import state_for

        assert state_for(app).counters == {}
        meters = await client.get(url + f"/meters?host_id={host}")
        assert (
            meters.json()["products"]["codex-native"]
            == meters.json()["products"]["codex"]
            == "codex"
        )
        assert meters.json()["floors"]["account-a"]["active"]
        assert (
            await client.post(url + "/floor", json={"tile": "missing", "off": True})
        ).status_code == 409
        assert (
            await client.post(
                url + "/floor", json={"tile": "account-a", "off": True, "keep": True}
            )
        ).json()["account-a"]["until"] == "manual"
        assert (
            await client.post(url + "/floor", json={"tile": "account-a", "off": False})
        ).json() == {}
        result = await client.put(url, json={"settings": settings, "expected_revision": revision})
        assert result.status_code == 200, result.text


@pytest.mark.asyncio
@pytest.mark.parametrize("tile", ["claude", "claude-pro"])
async def test_floor_until_reset_with_board_disabled_reads_host_scoped_report(
    db_uri, tmp_path, tile
):
    import time

    app = _preferences_app(db_uri, tmp_path)
    service = app.state.agent_quota_service
    service.usage_enabled = False
    now = time.time()
    service.report(
        OWNER,
        "host-a",
        "claude",
        {
            "captured_at": now,
            "windows": [
                {
                    "label": "7d",
                    "used_percent": 90,
                    "duration_mins": 10080,
                    "resets_at": now + 86400,
                },
            ],
        },
        now=now,
    )
    if tile != "claude":
        from omnigent.usage.contract import UsageReport

        service.report_account(
            OWNER,
            tile,
            UsageReport.model_validate(
                {
                    "observed_at": now,
                    "windows": [
                        {
                            "id": "7d",
                            "percent_used": 90,
                            "window_minutes": 10080,
                            "resets_at": now + 86400,
                        }
                    ],
                }
            ),
            now=now,
        )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"X-Test-User": OWNER},
    ) as client:
        floor = await client.post("/v1/me/session-choice/floor", json={"tile": tile, "off": True})
    assert floor.status_code == 200, floor.text
    assert floor.json()[tile]["windows"]["7d"] == [now + 86400, 90]


NOW = 2_000_000


def table(weights=(2, 1), *, strategy="drain", tiers=None, **changes):
    rows = [
        {
            "preset": f"p{index}",
            "weight": weight,
            "links": [
                {"agent": f"agent-{index}", "model": f"model-{index}", "meter": f"pool-{index}"}
            ],
        }
        for index, weight in enumerate(weights)
    ]
    return parse_session_choice(
        {
            "enabled": True,
            "strategy": strategy,
            "workTypes": [{"id": "work", "label": "Work"}],
            "presets": [
                {"id": row["preset"], "name": row["preset"], "links": row["links"]} for row in rows
            ],
            "hosts": {"host-a": {"work": tiers or [rows]}},
            **changes,
        }
    )


def reading(pool="pool-0", *, used=40, days=4, state="ok", pace=70, freshness="fresh"):
    return UsageAccountView(
        id=pool,
        kind="account",
        display_name=pool,
        state=state,
        pace_pct=pace,
        freshness=freshness,
        windows=[
            UsageWindowView(
                id="7d",
                kind="weekly",
                percent_used=used,
                remaining_pct=100 - used,
                window_minutes=10080,
                resets_at=NOW + days * 86400,
                state=state,
            )
        ],
    )


def pick(
    settings,
    *,
    counters=None,
    readings=None,
    running=None,
    floors=None,
    request=None,
    name="work",
    now=NOW,
):
    return choose(
        settings,
        name,
        request or ChoiceRequest(OWNER, "host-a"),
        readings or {},
        running if running is not None else {},
        floors or {},
        {},
        counters if counters is not None else {},
        now=now,
    )


def fallback_table(*, second_mode="drain", third_mode="return", **changes):
    links = [
        {"agent": "official", "model": "gpt-official", "meter": "pool-0"},
        {
            "agent": "alternate",
            "model": "gpt-alternate",
            "meter": "pool-1",
            "fallbackMode": second_mode,
        },
        {"agent": "other", "model": "claude-other", "meter": "pool-2", "fallbackMode": third_mode},
    ]
    return table(tiers=[[{"links": links}]], **changes)


def test_drain_matches_route_reconciliation_sequence():
    settings = fallback_table()
    revision = choice_revision(choice_document(settings))
    request = ChoiceRequest(OWNER, "host-a")
    locks = {}
    steps = [
        ((0,), "pool-1", True, False),
        ((), "pool-1", False, False),
        ((0, 1), "pool-2", False, True),
        ((0,), "pool-1", True, False),
        ((), "pool-1", False, False),
        ((1,), "pool-0", False, True),
        ((), "pool-0", False, False),
    ]
    for index, (closed, expected, sets, drops) in enumerate(steps):
        readings = {f"pool-{n}": reading(f"pool-{n}", used=100) for n in closed}
        counters = {}
        result = plan_choice(
            settings,
            "work",
            request,
            readings,
            {},
            {},
            {},
            counters,
            now=NOW + index,
            drain_locks=locks,
            settings_revision=revision,
        )
        assert counters == {}
        assert result.choice.pool == expected
        assert bool(result.lock) == sets
        assert bool(result.drops) == drops
        for lock in result.drops:
            assert locks.pop(lock.key) == lock
        if result.lock:
            locks[result.lock.key] = result.lock
        if locks and not sets:
            assert "Continuing to drain" in result.choice.reason
            assert next(iter(locks.values())).since < NOW + index


def test_drain_uses_reserve_but_ignores_busy_and_return():
    limits = {"pool-0": {"maxRunning": 1, "reserve": {"mode": "total", "window": "7d", "pct": 20}}}
    settings = fallback_table(limits=limits)
    request = ChoiceRequest(OWNER, "host-a")
    busy = plan_choice(
        settings,
        "work",
        request,
        {},
        {"pool-0": ("running",)},
        {},
        {},
        {},
        now=NOW,
        drain_locks={},
    )
    assert busy.choice.pool == "pool-1" and busy.lock is None
    assert busy.choice.skipped[0].why == "busy"
    reserved = {"pool-0": reading(used=90)}
    drained = plan_choice(
        settings, "work", request, reserved, {}, {}, {}, {}, now=NOW, drain_locks={}
    )
    assert drained.choice.skipped[0].why == "reserve" and drained.lock is not None
    for returned_readings, expected in ((reserved, "pool-1"), ({}, "pool-0")):
        returned = plan_choice(
            fallback_table(second_mode="return", limits=limits),
            "work",
            request,
            returned_readings,
            {},
            {},
            {},
            {},
            now=NOW,
            drain_locks={},
        )
        assert returned.choice.pool == expected and returned.lock is None


def test_drain_keeps_request_filtered_lock_until_actual_closure():
    settings = fallback_table(third_mode="drain", limits={"pool-2": {"maxRunning": 1}})
    request = ChoiceRequest(OWNER, "host-a")
    stopped = {f"pool-{n}": reading(f"pool-{n}", used=100) for n in (0, 1)}
    first = plan_choice(
        settings, "work", request, stopped, {}, {}, {}, {}, now=NOW, drain_locks={}
    )
    lock = first.lock
    assert lock is not None and first.choice.pool == "pool-2"
    readings = {"pool-0": stopped["pool-0"]}

    def retry(request, *, running=None, cooldowns=None, extra_readings=None):
        return plan_choice(
            settings,
            "work",
            request,
            readings | (extra_readings or {}),
            running or {},
            {},
            cooldowns or {},
            {},
            now=NOW + 1,
            drain_locks={lock.key: lock},
        )

    caller_filter = replace(request, differ_family=model_family("claude-other"))
    busy = {"pool-2": ("running",)}
    preview = replace(request, preview=True, exclude=("pool-2",))
    for result, skipped in (
        (retry(request, running=busy), "busy"),
        (retry(caller_filter), "same_family"),
        (retry(preview), "excluded"),
    ):
        assert result.choice.pool == "pool-1"
        assert result.choice.skipped[0].why == skipped
        assert result.drops == () and result.lock is None
    assert retry(request).choice.pool == "pool-2"
    for result in (
        retry(request, running=busy, cooldowns={"pool-2": NOW + 40}),
        retry(caller_filter, extra_readings={"pool-2": reading("pool-2", used=100)}),
        retry(replace(request, link_exclusions={"other": NOW + 40})),
    ):
        assert result.choice.pool == "pool-1"
        assert result.drops == (lock,)
        assert result.lock is not None and result.lock.pool == "pool-1"


def test_drain_lock_save_validates_saved_rows_and_preserves_since(db_uri):
    store = SqlAlchemyUserPreferencesStore(db_uri)
    document = cast(dict[str, Any], choice_document(fallback_table()))
    original = document["hosts"]["host-a"]["work"][0][0]["links"]
    document["hosts"]["host-a"]["work"].append(
        [{"links": [{"agent": "different-head"}, original[1]]}]
    )
    saved = save(store, document)
    snapshot = store.session_choice_snapshot(OWNER)
    planned = plan_choice(
        parse_session_choice(saved["settings"]),
        "work",
        ChoiceRequest(OWNER, "host-a"),
        {"pool-0": reading(used=100)},
        {},
        {},
        {},
        {},
        now=NOW,
        drain_locks=snapshot.locks,
        settings_revision=snapshot.revision,
    )
    lock = planned.lock
    assert lock is not None

    def replace_links(links, *, other_row=None, swap=False):
        changed = deepcopy(saved["settings"])
        tiers = changed["hosts"]["host-a"]["work"]
        tiers[0][0]["links"] = links
        if other_row is not None:
            tiers[1][0]["links"] = other_row
        if swap:
            tiers.reverse()
        return save(store, changed)

    assert store.apply_drain_lock_delta(OWNER, snapshot, admitted=lock)
    assert store.session_choice_snapshot(OWNER).revision == snapshot.revision
    swap_inside_row = replace_links([original[0], original[2], original[1]])
    after = store.session_choice_snapshot(OWNER)
    assert after.locks[lock.key].since == NOW
    assert after.locks[lock.key].settings_revision == swap_inside_row["revision"]

    for links, other_row, swap in (
        (original, None, True),
        ([{"agent": "preset-head"}, *original[1:]], None, False),
        ([*original, original[1]], None, False),
        (original, original, False),
        ([original[1], original[0], original[2]], None, False),
        ([original[0], {**original[1], "fallbackMode": "return"}, original[2]], None, False),
    ):
        save(store, saved["settings"])
        current = store.session_choice_snapshot(OWNER)
        current_lock = lock.model_copy(update={"settings_revision": current.revision})
        store.apply_drain_lock_delta(OWNER, current, admitted=current_lock)
        assert store.session_choice_snapshot(OWNER).locks
        replace_links(links, other_row=other_row, swap=swap)
        assert store.session_choice_snapshot(OWNER).locks == {}
        save(store, saved["settings"])
        assert store.session_choice_snapshot(OWNER).revision == current.revision
        assert not store.apply_drain_lock_delta(OWNER, current, admitted=current_lock)
        assert store.session_choice_snapshot(OWNER).locks == {}


def test_drain_proposal_belongs_to_weighted_winner():
    settings = cast(dict[str, Any], choice_document(fallback_table()))
    tier = settings["hosts"]["host-a"]["work"][0]
    other = deepcopy(tier[0])
    other["weight"] = 4
    other["links"][1].update(agent="weighted", model="gpt-weighted", meter="pool-3")
    tier.append(other)
    plan = plan_choice(
        parse_session_choice(settings),
        "work",
        ChoiceRequest(OWNER, "host-a"),
        {"pool-0": reading(used=100)},
        {},
        {},
        {},
        {},
        now=NOW,
        drain_locks={},
    )
    assert plan.choice.pool == "pool-3"
    assert plan.lock is not None and plan.lock.key == ("host-a", "work", 0, 1)


def test_drain_lock_merges_simultaneous_owner_host_writes(db_uri):
    settings = cast(dict[str, Any], choice_document(fallback_table()))
    settings["hosts"]["host-b"] = deepcopy(settings["hosts"]["host-a"])
    store = SqlAlchemyUserPreferencesStore(db_uri)
    save(store, settings)
    snapshot = store.session_choice_snapshot(OWNER)

    def admit(host):
        plan = plan_choice(
            parse_session_choice(snapshot.document),
            "work",
            ChoiceRequest(OWNER, host),
            {"pool-0": reading(used=100)},
            {},
            {},
            {},
            {},
            now=NOW,
            drain_locks=snapshot.locks,
            settings_revision=snapshot.revision,
        )
        assert plan.lock is not None
        return store.apply_drain_lock_delta(OWNER, snapshot, admitted=plan.lock)

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert all(executor.map(admit, ("host-a", "host-b")))
    after = SqlAlchemyUserPreferencesStore(db_uri).session_choice_snapshot(OWNER)
    assert set(after.locks) == {("host-a", "work", 0, 0), ("host-b", "work", 0, 0)}
    assert after.revision == snapshot.revision
    assert store.session_choice_snapshot("other@example.test").locks == {}


def test_exhausted_without_reset_is_skipped_without_an_invented_until():
    exhausted = reading(used=100)
    exhausted.windows[0].resets_at = None
    result = pick(table(), readings={"pool-0": exhausted})
    assert result.pool == "pool-1"
    assert result.skipped[0].why == "exhausted"
    assert result.skipped[0].until is None


@pytest.mark.parametrize(
    "message,seconds",
    [
        ("Individual quota reached. Resets in 132h0m26s", 475226),
        ("Error: Quota exceeded. Resets in 1d 2h 3m 4s.", 93784),
        ("Individual quota reached. Resets in 20m", 1200),
        ("Individual quota reached. Resets in 0s", None),
        ("Individual quota reached. Resets in -1h", None),
        ("Individual quota reached. Resets in 1.5h", None),
        ("Individual quota reached. Resets in 9999h", None),
        ("Individual quota reached. Resets in 12hours", None),
        ("Individual quota reached. Resets in unknown", None),
        ("HTTP 429: too many requests", None),
        ("The docs say: Individual quota reached. Resets in 1h", None),
    ],
)
def test_explicit_provider_quota_reset(message, seconds):
    assert quota_reset_at(message, NOW) == (NOW + seconds if seconds else None)


CC_SWITCH_LIVE_REFUSAL = (
    "unexpected status 403 Forbidden: CC Switch local proxy failed ... "
    "Provider: OpenAI Official; model: gpt-6.2-sol; cause: Codex 官方额度保护: "
    "每周 98.00% ≥ 98%（重置 2026-11-22T03:19:03Z）"
)


@pytest.mark.parametrize(
    "message,reset",
    [
        (
            "unexpected status 403 Forbidden: CC Switch local proxy failed ... "
            "Provider: OpenAI Official; model: gpt-6.2-sol; cause: Codex 官方额度保护 "
            "(quota protection): 每周 (weekly): 98.00% ≥ 98%（重置 / reset: "
            "2026-11-22T03:19:03+00:00）...",
            "2026-11-22T03:19:03+00:00",
        ),
        (
            "unexpected status 403 Forbidden: CC Switch local proxy failed ... "
            "Provider: OpenAI Official; model: gpt-6.2-sol; cause: Codex 官方额度保护: "
            "每周 98.00% ≥ 98%（重置 2026-11-22T03:19:03Z）",
            "2026-11-22T03:19:03+00:00",
        ),
        (
            "unexpected status 403 Forbidden: CC Switch local proxy failed ... "
            "cause: Codex quota protection: weekly 98.00% ≥ 98% "
            "(reset: 2026-11-22T03:19:03Z)",
            "2026-11-22T03:19:03+00:00",
        ),
        (
            "Claude Code terminal did not become ready within 30.1s (...). "
            "The message was not delivered. Last terminal output:\n" + CC_SWITCH_LIVE_REFUSAL,
            None,
        ),
        (
            "Claude Code terminal did not become ready within 30.1s (...). "
            "The message was not delivered. Last captured terminal output:\n"
            + CC_SWITCH_LIVE_REFUSAL,
            None,
        ),
        (
            "unexpected status 403 Forbidden: CC Switch local proxy failed ... "
            "cause: Codex quota protection: weekly 98.00% ≥ 98% "
            "(reset: 2027-11-19T03:19:03+00:00)",
            "2027-11-19T03:19:03+00:00",
        ),
        (
            "unexpected status 403 Forbidden: CC Switch local proxy failed ... "
            "cause: Codex quota protection: weekly; detail: retry later; "
            "reset: 2026-11-22T03:19:03Z",
            None,
        ),
        (
            "unexpected status 403 Forbidden: CC Switch local proxy failed ... "
            "cause: Codex 官方额度保护: 每周\n重置: 2026-11-22T03:19:03Z",
            None,
        ),
        (
            "unexpected status 403 Forbidden: CC Switch local proxy failed ... "
            "cause: Codex 官方额度保护: 每周 98.00% ≥ 98%（重置:\n"
            "2026-11-22T03:19:03Z）",
            None,
        ),
        (
            "unexpected status 403 Forbidden: CC Switch local proxy failed ... "
            "cause: Codex 官方额度保护: 每周 98.00% ≥ 98%（重置\n"
            "2026-11-22T03:19:03Z）",
            None,
        ),
        (
            "unexpected status 403 Forbidden: CC Switch local proxy failed ... "
            "cause: Codex quota protection: weekly 98.00% ≥ 98% "
            "(reset\n:2026-11-22T03:19:03Z)",
            None,
        ),
        (
            "unexpected status 403 Forbidden: CC Switch local proxy failed ... "
            '{"cause":"Codex quota protection: weekly","reset":"2026-11-22T03:19:03Z"}',
            None,
        ),
        (
            "unexpected status 403 Forbidden: CC Switch local proxy failed ... "
            "cause: Codex quota protection: weekly (reset: 2026-11-22T03:19:03)",
            None,
        ),
        (
            "unexpected status 403 Forbidden: CC Switch local proxy failed ... "
            "cause: Codex quota protection: weekly. "
            "Account metadata reset: 2026-11-22T03:19:03Z",
            None,
        ),
        (
            "unexpected status 500 Internal Server Error: upstream returned "
            '"unexpected status 403 Forbidden: Codex quota protection: weekly '
            '98.00% ≥ 98% (reset: 2026-11-22T03:19:03Z)"',
            None,
        ),
        (
            "unexpected status 403 Forbidden: Other Provider local proxy failed ... "
            "cause: Codex quota protection: weekly 98.00% ≥ 98% "
            "(reset: 2026-11-22T03:19:03Z)",
            None,
        ),
        (
            "unexpected status 403 Forbidden: quota exceeded; reset: 2026-11-22T03:19:03+00:00",
            None,
        ),
        (
            "unexpected status 403 Forbidden: CC Switch local proxy failed ... "
            "cause: Codex quota protection: weekly (reset: 2026-11-18T03:19:02+00:00)",
            None,
        ),
        (
            "unexpected status 403 Forbidden: CC Switch local proxy failed ... "
            "cause: Codex quota protection: weekly (reset: 2027-11-20T03:19:03+00:00)",
            None,
        ),
    ],
)
def test_quota_protection_absolute_reset(message, reset):
    now = datetime.fromisoformat("2026-11-18T03:19:03+00:00").timestamp()
    expected = datetime.fromisoformat(reset).timestamp() if reset else None
    assert quota_reset_at(message, now) == expected


def test_provider_rejection_overrides_balance_for_only_its_owner_until_reset():
    from omnigent.server.session_choice import TileLimits, tile_line
    from omnigent.usage.contract import UsageBalance

    state = ChoiceState()
    state.exhausted[(OWNER, "pool-0")] = NOW + 60
    quota = reading()
    quota.balances = [UsageBalance(id="credit", remaining=20, unit="USD")]
    quota.balance_freshness = "fresh"
    readings = {"pool-0": quota}
    for preview in (True, False):
        result = state.select(
            table((1,)),
            "work",
            ChoiceRequest(OWNER, "host-a", preview=preview),
            readings,
            {},
            {"pool-0": FloorOff(until="manual")},
            now=NOW,
        )
        assert result.outcome == "hold"
        assert result.reason == "exhausted"
        assert result.clears_at == NOW + 60
    assert tile_line(TileLimits(), quota, None, NOW).state == "ok"
    other = state.select(
        table((1,)),
        "work",
        ChoiceRequest("other@example.test", "host-a"),
        readings,
        {},
        {},
        now=NOW,
    )
    assert other.outcome == "pick"
    assert (
        state.select(
            table((1,)),
            "work",
            ChoiceRequest(OWNER, "host-a"),
            readings,
            {},
            {},
            now=NOW + 60,
        ).outcome
        == "pick"
    )


@pytest.mark.parametrize("strategy", ["drain", "balance"])
def test_weighted_picks_and_preview(strategy):
    settings = table(strategy=strategy)
    counters = {}
    result = [pick(settings, counters=counters).pool for _ in range(9)]
    assert result == ["pool-0", "pool-1", "pool-0"] * 3
    saved = dict(counters)
    preview = ChoiceRequest(OWNER, "host-a", preview=True)
    assert pick(settings, counters=counters, request=preview).pool == "pool-0"
    assert pick(settings, counters=counters, request=preview).pool == "pool-0"
    assert counters == saved
    assert pick(table((3, 2, 1)), counters={}).pool == "pool-0"
    counters = {}
    result = [pick(table((3, 2, 1)), counters=counters).pool for _ in range(30)]
    assert [result.count(f"pool-{i}") for i in range(3)] == [15, 10, 5]


def test_limit_before_weights_and_next_tier():
    settings = table((4, 3, 2), limits={"pool-2": {"maxRunning": 1}})
    counters = {}
    result = []
    for _ in range(9):
        running = {"pool-2": ("child-c",)} if "pool-2" in result else {}
        result.append(pick(settings, counters=counters, running=running).pool)
    assert result == [
        "pool-0",
        "pool-1",
        "pool-2",
        "pool-0",
        "pool-1",
        "pool-0",
        "pool-0",
        "pool-1",
        "pool-0",
    ]
    rows = table().model_dump(mode="json", by_alias=True)["hosts"]["host-a"]["work"][0]
    settings = table(tiers=[[rows[0]], [rows[1]]], limits={"pool-0": {"maxRunning": 1}})
    chosen = pick(settings, running={"pool-0": ("child-a",)})
    assert chosen.pool == "pool-1" and chosen.tier == 2
    assert chosen.skipped[0].running == ("child-a",)


def test_chain_keeps_weight_and_row_positions_are_separate():
    raw = table().model_dump(mode="json", by_alias=True)
    tier = raw["hosts"]["host-a"]["work"][0]
    tier[0]["links"].append({"agent": "agent-0", "model": "provider/other"})
    tier[1] = {**deepcopy(tier[0]), "weight": 1}
    tier[1]["links"][0]["meter"] = "pool-1"
    settings = parse_session_choice(raw)
    counters = {}
    pools = []
    for index in range(9):
        picked = pick(
            settings,
            counters=counters,
            readings={"pool-0": reading(used=100)} if index > 3 else {},
        )
        if index > 3 and picked.pool != "pool-1":
            assert picked.link.model == "provider/other"
        pools.append(picked.pool)
    assert pools.count("pool-1") == 3
    assert {key[-1] for key in counters} == {0, 1}
    assert len(counters) == 2


def test_daily_reserve_releases_before_reset_and_total_waits():
    settings = table(limits={"pool-0": {"reserve": {"mode": "daily", "window": "7d", "pct": 5}}})
    quota = reading(used=74.5, days=4.9)
    assert pick(settings, readings={"pool-0": quota}).pool == "pool-0"
    quota = reading(used=75.5, days=4.9)
    skipped = pick(settings, readings={"pool-0": quota})
    assert skipped.pool == "pool-1" and skipped.skipped[0].why == "reserve"
    assert skipped.skipped[0].until == NOW + 0.9 * 86400
    assert pick(settings, readings={"pool-0": quota}, now=NOW + 1).pool == "pool-1"
    assert pick(settings, readings={"pool-0": quota}, now=NOW + 86400).pool == "pool-0"
    total = table(limits={"pool-0": {"reserve": {"mode": "total", "window": "7d", "pct": 20}}})
    quota = reading(used=81)
    assert pick(total, readings={"pool-0": quota}).pool == "pool-1"
    assert pick(total, readings={"pool-0": quota}, now=NOW + 86400).pool == "pool-1"


@pytest.mark.parametrize("mode", ["daily", "total"])
def test_exempt_and_floor_release_never_ignore_exhaustion(mode):
    settings = table(
        limits={
            "pool-0": {
                "reserve": {"mode": mode, "window": "7d", "pct": 30},
                "reserveExempt": ["work"],
            }
        }
    )
    for floors in ({}, {"pool-0": FloorOff(until="manual")}):
        assert (
            pick(settings, readings={"pool-0": reading(used=99)}, floors=floors).pool == "pool-0"
        )
        exhausted = pick(settings, readings={"pool-0": reading(used=100)}, floors=floors)
        assert exhausted.pool == "pool-1" and exhausted.skipped[0].why == "exhausted"


def test_floor_reset_detection_and_use_up():
    entry = FloorOff(windows={"7d": (NOW + 4 * 86400, 80)})
    assert floor_off_active(entry, None, NOW)
    assert floor_off_active(entry, reading(used=80), NOW)
    assert not floor_off_active(entry, reading(used=74), NOW)
    assert not floor_off_active(entry, reading(used=80, days=5), NOW)
    assert not floor_off_active(entry, reading(used=80), NOW + 4 * 86400)
    assert floor_off_active(entry, reading(used=80, days=4 + 600 / 86400), NOW)
    empty = reading().model_copy(update={"windows": []})
    assert not floor_off_active(entry, empty, NOW)
    assert floor_off_active(FloorOff(until="manual"), empty, NOW + 30 * 86400)


def test_unknown_stale_balance_and_reversed_low_pace():
    settings = table(strategy="balance")
    for data in ({}, {"pool-0": reading(used=100, freshness="old")}):
        assert pick(settings, readings=data).pool == "pool-0"
    assert pick(settings, readings={"pool-0": reading(state="low", pace=5)}).pool == "pool-1"
    assert (
        pick(
            settings,
            readings={
                "pool-0": reading(state="low", pace=5),
                "pool-1": reading("pool-1", state="low", pace=10),
            },
        ).pool
        == "pool-1"
    )


def test_hold_and_ask_do_not_bind_unavailable_or_excluded():
    settings = table((1,), limits={"pool-0": {"maxRunning": 1}})
    held = pick(settings, running={"pool-0": ("running",)})
    assert held.outcome == "hold" and held.pool == "pool-0"
    assert (
        pick(
            settings,
            running={"pool-0": ("running",)},
            request=ChoiceRequest(OWNER, "host-a", independent=True),
        ).outcome
        == "ask"
    )
    for request in (
        ChoiceRequest(OWNER, "host-a", unavailable=frozenset({"agent-0"})),
        ChoiceRequest(OWNER, "host-a", exclude=("pool-0",)),
    ):
        assert pick(settings, request=request).outcome == "ask"
    assert pick(settings, request=ChoiceRequest(OWNER, "host-b")).outcome == "ask"


def test_availability_ability_pin_family_and_type_resolution():
    raw = table().model_dump(mode="json", by_alias=True)
    raw["abilityOrder"] = [["p0"], ["p1"]]
    raw["workTypes"][0]["aliases"] = ["old-work"]
    settings = parse_session_choice(raw)
    assert pick(settings, name="old-work").flags == ()
    assert pick(settings, name="missing").flags == ("unknown_work_type",)
    assert pick(settings, name=None).flags == ("work_type_omitted",)
    above = ChoiceRequest(OWNER, "host-a", caller_product="agent-1", caller_model="model-1")
    assert pick(settings, request=above).skipped[0].why == "above_caller"
    unavailable = replace(above, caller_product=None, models={"agent-0": frozenset({"other"})})
    assert pick(settings, request=unavailable).pool == "pool-0"
    family = replace(above, caller_product=None, differ_family="model-0")
    assert pick(settings, request=family).skipped[0].why == "same_family"
    assert pick(settings, request=ChoiceRequest(OWNER, "host-a", pin="p1")).pool == "pool-1"
    assert pick(settings, request=replace(above, pin="p0")).outcome == "ask"


def test_no_meter_limit_and_two_windows_first_reset():
    raw = table((1,)).model_dump(mode="json", by_alias=True)
    link = raw["hosts"]["host-a"]["work"][0][0]["links"][0]
    link.pop("meter")
    link["maxRunning"] = 2
    settings = parse_session_choice(raw)
    assert pick(settings).pool == "agent-0:model-0"
    assert pick(settings, running={"agent-0:model-0": ("one", "two")}).outcome == "hold"
    quota = reading(used=90)
    quota.windows.append(
        UsageWindowView(
            id="5h",
            kind="session",
            percent_used=100,
            remaining_pct=0,
            resets_at=NOW + 300,
            state="exhausted",
        )
    )
    exhausted = pick(table(), readings={"pool-0": quota})
    assert exhausted.skipped[0].until == NOW + 300
    entry = FloorOff(windows={"7d": (NOW + 4 * 86400, 90), "5h": (NOW + 300, 100)})
    assert not floor_off_active(entry, quota, NOW + 300)


def test_host_counters_saves_and_global_cooldown():
    raw = table((1, 1)).model_dump(mode="json", by_alias=True)
    raw["hosts"]["host-b"] = deepcopy(raw["hosts"]["host-a"])
    settings = parse_session_choice(raw)
    state = ChoiceState()

    def select(host, **kwargs):
        return state.select(
            settings, "work", ChoiceRequest(OWNER, host, **kwargs), {}, {}, {}, now=NOW
        )

    assert [select(host).pool for host in ("host-a", "host-b", "host-a", "host-b")] == [
        "pool-0",
        "pool-0",
        "pool-1",
        "pool-1",
    ]
    select("host-a", exclude=("pool-0",))
    assert select("host-b").pool == "pool-1"
    changed = deepcopy(raw)
    changed["hosts"]["host-a"]["work"][0].reverse()
    state.saved(OWNER, parse_session_choice(changed))
    assert not any(key[1] == "host-a" for key in state.counters)
    assert any(key[1] == "host-b" for key in state.counters)


def test_exact_attribution_order_label_and_model_families():
    raw = document()
    raw["hosts"]["host-a"]["implementation"][0][0]["links"][0]["meter"] = "old-meter"
    settings = parse_session_choice(raw)
    conv = Conversation(
        id="child",
        created_at=NOW,
        updated_at=NOW,
        root_conversation_id="child",
        model_override="gpt-example",
    )
    assert pool_of_session(conv, settings, product="codex") == "account-a"
    conv.labels[CHOICE_LABEL] = '{"pool":"old-meter"}'
    assert pool_of_session(conv, settings, product="codex") == "old-meter"
    conv.labels = {}
    assert pool_of_session(conv, settings, product="codex", model="different") is None
    assert [
        model_family(name)
        for name in (
            "opus",
            "ccs-provider/gpt-model",
            "litellm/llm_kimi_x",
            "gemini-x",
            "litellm/llm_deepseek_flash",
            "glm-5",
        )
    ] == ["claude", "gpt", "kimi", "gemini", "deepseek", "glm-5"]


@pytest.mark.parametrize(
    "caller_model", ["gpt-small", "official/gpt-small", "krill/gpt-small", "ccs-krill/gpt-small"]
)
def test_codex_ability_cap_compares_route_pins_by_plain_model(caller_model):
    raw = table().model_dump(mode="json", by_alias=True)
    raw["abilityOrder"] = [["p0"], ["p1"]]
    rows = raw["hosts"]["host-a"]["work"][0]
    for index, model in enumerate(("gpt-large", "gpt-small")):
        link = {"agent": f"agent-{index}", "model": model, "meter": "codex-pro"}
        rows[index]["links"] = [link]
        raw["presets"][index]["links"] = [link]
    choice = pick(
        parse_session_choice(raw),
        request=ChoiceRequest(
            OWNER,
            "host-a",
            products={"agent-0": "codex", "agent-1": "codex"},
            caller_product="codex",
            caller_model=caller_model,
        ),
    )
    assert choice.link.model == "gpt-small"
    assert choice.pool == "codex-pro"
    assert choice.skipped[0].why == "above_caller"


def test_codex_route_pins_select_by_tile_with_plain_catalog_models():
    raw = table().model_dump(mode="json", by_alias=True)
    rows = raw["hosts"]["host-a"]["work"][0]
    for row, route in zip(rows, ("official", "krill"), strict=True):
        row["links"][0] = {"agent": row["links"][0]["agent"], "model": f"{route}/gpt-example"}
    raw["limits"] = {
        "codex-pro": {
            "reserve": {"mode": "total", "window": "7d", "pct": 15, "stopPct": 5},
            "maxRunning": 1,
        },
        "krill": {"maxRunning": 1},
    }
    # A router that offers route pins lists the pinned ids themselves.
    pinned = frozenset({"gpt-example", "official/gpt-example", "krill/gpt-example"})
    request = ChoiceRequest(
        OWNER,
        "host-a",
        products={"agent-0": "codex", "agent-1": "codex"},
        models=dict.fromkeys(("agent-0", "agent-1"), pinned),
    )
    settings = parse_session_choice(raw)
    selected = pick(settings, request=request)
    assert (selected.pool, selected.link.model) == ("codex-pro", "official/gpt-example")
    assert selected.warnings == ()
    warned = pick(settings, request=request, readings={"codex-pro": reading(used=90)})
    assert (warned.pool, warned.link.model) == ("krill", "krill/gpt-example")
    assert warned.skipped[0].why == "reserve"
    raw["limits"]["codex-pro"]["reserveExempt"] = ["work"]
    exempt = parse_session_choice(raw)
    assert (
        pick(exempt, request=request, readings={"codex-pro": reading(used=90)}).pool == "codex-pro"
    )
    assert pick(exempt, request=request, readings={"codex-pro": reading(used=95)}).pool == "krill"
    busy = pick(settings, request=request, running={"codex-pro": ("official-child",)})
    assert busy.pool == "krill" and busy.skipped[0].why == "busy"


@pytest.mark.parametrize("meter", ["codex-pro", "krill"])
def test_codex_meter_keeps_the_link_model_as_written(meter):
    raw = table((1,)).model_dump(mode="json", by_alias=True)
    raw["hosts"]["host-a"]["work"][0][0]["links"][0] = {
        "agent": "agent-0",
        "model": "gpt-6.1-sol",
        "meter": meter,
    }
    raw["limits"] = {meter: {"maxRunning": 1}}
    settings = parse_session_choice(raw)
    request = ChoiceRequest(
        OWNER,
        "host-a",
        products={"agent-0": "codex"},
        models={"agent-0": frozenset({"official/gpt-6.1-sol"})},
    )
    for running, outcome in (({}, "pick"), ({meter: ("child",)}, "hold")):
        choice = pick(settings, request=request, running=running)
        assert (choice.outcome, choice.pool, choice.link.model) == (
            outcome,
            meter,
            "gpt-6.1-sol",
        )


@pytest.mark.parametrize("route,pool", [("official", "codex-pro"), ("krill", "krill")])
def test_explicit_codex_route_pin_keeps_its_launch_model(route, pool):
    raw = table((1,)).model_dump(mode="json", by_alias=True)
    raw["hosts"]["host-a"]["work"][0][0]["links"][0] = {
        "agent": "agent-0",
        "model": f"{route}/gpt-6.1-sol",
    }
    raw["limits"] = {pool: {"maxRunning": 1}}
    settings = parse_session_choice(raw)
    request = ChoiceRequest(OWNER, "host-a", products={"agent-0": "codex"})
    for running, outcome in (({}, "pick"), ({pool: ("child",)}, "hold")):
        choice = pick(settings, request=request, running=running)
        assert (choice.outcome, choice.pool, choice.link.model) == (
            outcome,
            pool,
            f"{route}/gpt-6.1-sol",
        )


@pytest.mark.parametrize(
    "route,pool", [("official", "codex-pro"), ("krill", "krill"), ("ccs-krill", "krill")]
)
def test_codex_pin_attribution_survives_plain_reports_and_stale_meter(route, pool):
    conv = Conversation(
        id="child",
        created_at=NOW,
        updated_at=NOW,
        root_conversation_id="child",
        model_override=f"{route}/gpt-example",
        reported_model="gpt-example",
        labels={CHOICE_LABEL: '{"pool":"outdated-meter"}'},
    )
    assert pool_of_session(conv, parse_session_choice({}), product="codex") == pool
    conv.labels[CHOICE_LABEL] = f'{{"pool":"outdated-meter","model":"{route}/gpt-example"}}'
    conv.model_override = None
    assert pool_of_session(conv, parse_session_choice({}), product="codex") == pool


@pytest.mark.parametrize(
    "product,model",
    [
        ("pi", "krill/gpt-example"),
        ("codex", "other/gpt-example"),
        ("codex", "ccs-krill/gpt-example"),
        ("codex", "official/gpt-example"),
    ],
)
def test_catalog_has_no_alias_for_a_configured_link_model(product, model):
    # A catalog miss is unknown, not a refusal: only a real launch failure excludes a
    # model. A route pin is likewise launched as written, never as its bare id.
    raw = table((1,)).model_dump(mode="json", by_alias=True)
    raw["hosts"]["host-a"]["work"][0][0]["links"][0]["model"] = model
    result = pick(
        parse_session_choice(raw),
        request=ChoiceRequest(
            OWNER,
            "host-a",
            products={"agent-0": product},
            models={"agent-0": frozenset({"gpt-example"})},
        ),
    )
    assert result.outcome == "pick"
    assert result.link.model == model
    assert result.notes[0]["outcome"] == "missing"
    assert result.notes[0]["saved"] == model
    assert result.warnings
    assert result.skipped == ()


def test_provider_rename_keeps_selection_limits_running_and_cooldown_identity():
    from omnigent.models.model_reference import provider_groups
    from omnigent.server.session_choice import RunningIdentity, RunningSnapshot

    raw = table((1,)).model_dump(mode="json", by_alias=True)
    link = {"agent": "agent-0", "model": "old/x", "maxRunning": 1}
    raw["hosts"]["host-a"]["work"][0][0]["links"] = [link]
    raw["presets"][0]["links"] = [link]
    raw["limits"] = {"pi:old/x": {"maxRunning": 1}}
    request = ChoiceRequest(
        OWNER,
        "host-a",
        products={"agent-0": "pi"},
        models={"agent-0": frozenset({"new/x"})},
        provider_groups=provider_groups([("old", "new")]),
    )
    settings = parse_session_choice(raw)
    result = pick(settings, request=request)
    assert result.link.model == "new/x" and result.pool == "pi:new/x"
    assert result.notes[0]["saved"] == "old/x"
    busy = pick(settings, request=request, running={"pi:old/x": ("running",)})
    assert busy.outcome == "hold" and busy.reason == "busy"
    excluded = pick(settings, request=replace(request, exclude=("pi:old/x",)))
    assert excluded.outcome == "ask" and excluded.skipped[0].why == "excluded"

    scoped = parse_session_choice(
        {
            **raw,
            "schemaVersion": 2,
            "limitRules": [
                {"id": "model-cap", "agent": "agent-0", "model": "old/x", "maxRunning": 1}
            ],
        }
    )
    running = RunningSnapshot({}, (RunningIdentity("other", "agent-0", "host-a", "new/x"),))
    assert pick(scoped, request=request, running=running).reason == "busy"
    state = ChoiceState()
    state.cooldowns[(OWNER, "pi:old/x")] = NOW + 30
    cooled = state.select(settings, "work", request, {}, {}, {}, now=NOW)
    assert cooled.outcome == "hold" and cooled.reason == "cooldown"
    state.exhausted[(OWNER, "pi:old/x")] = NOW + 40
    exhausted = state.quota_readings(
        OWNER, {"pi:new/x": reading("pi:new/x")}, NOW, request.provider_groups
    )
    assert exhausted["pi:new/x"].state == "exhausted"


def test_ambiguous_catalog_miss_tries_the_saved_link_without_exclusion():
    raw = table((1,)).model_dump(mode="json", by_alias=True)
    raw["hosts"]["host-a"]["work"][0][0]["links"][0]["model"] = "old/x"
    result = pick(
        parse_session_choice(raw),
        request=ChoiceRequest(OWNER, "host-a", models={"agent-0": frozenset({"a/x", "b/x"})}),
    )
    assert result.outcome == "pick" and result.link.model == "old/x"
    assert result.skipped == () and result.notes[0]["outcome"] == "missing"
    assert any("passing it to the host unchanged" in warning for warning in result.warnings)


@pytest.mark.parametrize(
    "status,age,marker,expected",
    [
        ("running", 10, "", True),
        ("waiting", 10, "", False),
        ("running", 301, "", False),
        (None, None, f"admitted@host-a@pool-0@{NOW - 60}", True),
        (None, None, f"admitted@host-a@pool-0@{NOW - 121}", False),
        ("running", 10, "queued@host-a@pool-0", False),
    ],
)
def test_slot_states(status, age, marker, expected):
    conv = Conversation(
        id="child",
        created_at=NOW,
        updated_at=NOW,
        root_conversation_id="child",
        live_status=status,
        runner_last_seen=NOW - age if age is not None else None,
        labels={SLOT_LABEL: marker},
    )
    assert holds_slot(conv, NOW) == expected
    conv.archived = True
    assert not holds_slot(conv, NOW)


@pytest.mark.parametrize("values", [{}, {"pct": 5, "stopPct": 10}, {"stopPct": -1}])
def test_invalid_quota_lines(values):
    from omnigent.server.session_choice import Reserve

    with pytest.raises(ValidationError):
        Reserve.model_validate({"mode": "total", "window": "7d", **values})


@pytest.mark.parametrize(
    "mode,pct,stop,used,days,state,warn,cut",
    [
        ("total", 15, 5, 85.1, 2.5, "warn", 15, 5),
        ("total", 15, 5, 95, 2.5, "stop", 15, 5),
        ("total", None, 5, 97, 2.5, "stop", None, 5),
        ("total", 15, 0, 100, 2.5, "stop", 15, 0),
        ("daily", 4, 2, 92, 2.5, "warn", 12, 6),
        ("daily", 4, 2, 92, 2, "warn", 8, 4),
        ("daily", 4, 2, 92, 1.99, "warn", 8, 4),
        ("daily", 4, 2, 94, 2.5, "stop", 12, 6),
    ],
)
def test_tile_lines(mode, pct, stop, used, days, state, warn, cut):
    from omnigent.server.session_choice import TileLimits, tile_line

    limits = TileLimits.model_validate(
        {
            "reserve": {
                "mode": mode,
                "window": "7d",
                "pct": pct,
                "stopPct": stop,
            }
        }
    )
    quota = reading(used=used, days=days)
    line = tile_line(limits, quota, None, NOW)
    assert (line.state, line.warn_line_pct, line.stop_line_pct) == (state, warn, cut)
    if mode == "daily" and used == 92:
        assert line.clears_at == quota.windows[0].resets_at - 86400
    assert tile_line(limits, quota, FloorOff(until="manual"), NOW).state == (
        "exhausted" if used == 100 else "ok"
    )
    assert (
        tile_line(limits, quota.model_copy(update={"freshness": "old"}), None, NOW).state
        == "unknown"
    )
    quota.windows[0].window_minutes = 300
    if mode == "daily":
        assert tile_line(limits, quota, None, NOW).state == "ok"


@pytest.mark.parametrize(
    "windows,freshness,amount,expires,state",
    [
        (True, "fresh", 3, None, "ok"),
        (True, "old", 3, None, "exhausted"),
        (True, "fresh", 3, NOW, "exhausted"),
        (False, "fresh", 0, None, "exhausted"),
        (False, "fresh", 3, None, "ok"),
        (False, "old", 0, None, "unknown"),
    ],
)
def test_exhaustion_counts_balance_sections(windows, freshness, amount, expires, state):
    from omnigent.server.session_choice import TileLimits, tile_line
    from omnigent.usage.contract import UsageBalance

    quota = reading(used=100)
    if not windows:
        quota.windows = []
        quota.freshness = "none"
    quota.balances = [UsageBalance(id="credit", remaining=amount, unit="USD", expires_at=expires)]
    quota.balance_freshness = freshness
    assert tile_line(TileLimits(), quota, None, NOW).state == state
    assert tile_line(TileLimits(), quota, FloorOff(until="manual"), NOW).state == state


def test_stop_ignores_warning_exemption_and_is_holdable():
    settings = table(
        (1,),
        limits={
            "pool-0": {
                "reserve": {"mode": "total", "window": "7d", "pct": 15, "stopPct": 5},
                "reserveExempt": ["work"],
            }
        },
    )
    assert pick(settings, readings={"pool-0": reading(used=90)}).outcome == "pick"
    held = pick(settings, readings={"pool-0": reading(used=95)})
    assert held.outcome == "hold" and held.skipped[0].why == "stop"


def test_value_windows_derive_remaining_without_changing_percent():
    from omnigent.quota_pace import QuotaTiers
    from omnigent.server.agent_quota import AgentQuotaService
    from omnigent.server.session_choice import TileLimits, tile_line
    from omnigent.usage.contract import UsageWindow

    windows, _ = AgentQuotaService._window_views(
        [UsageWindow(id="credits", used_value=12, limit_value=10)],
        True,
        NOW,
        QuotaTiers(),
        None,
    )
    assert windows[0].percent_used is None
    assert windows[0].remaining_pct == pytest.approx(-20)
    quota = reading().model_copy(update={"windows": windows})
    assert tile_line(TileLimits(), quota, None, NOW).state == "exhausted"


@pytest.mark.parametrize("used,state", [(85, "warn"), (95, "stop"), (100, "stop")])
def test_value_window_line_equality(used, state):
    from omnigent.quota_pace import QuotaTiers
    from omnigent.server.agent_quota import AgentQuotaService
    from omnigent.server.session_choice import TileLimits, tile_line
    from omnigent.usage.contract import UsageWindow

    windows, _ = AgentQuotaService._window_views(
        [UsageWindow(id="7d", used_value=used, limit_value=100)],
        True,
        NOW,
        QuotaTiers(),
        None,
    )
    quota = reading().model_copy(update={"windows": windows})
    limits = TileLimits.model_validate(
        {
            "reserve": {
                "mode": "total",
                "window": "7d",
                "pct": 15,
                "stopPct": 5,
            }
        }
    )
    assert tile_line(limits, quota, None, NOW).state == state


SHARED_LINKS = (
    {"agent": "agent-0", "model": "model-0", "meter": "pool-0"},
    {"agent": "agent-1", "model": "model-1", "meter": "pool-0"},
)


def shared_pool(*links, strategy="balance", **changes):
    """One balance tier whose links all report into the same pool."""
    raw = table(tuple(1 for _ in links), strategy=strategy, **changes).model_dump(
        mode="json", by_alias=True
    )
    for row, link in zip(raw["hosts"]["host-a"]["work"][0], links, strict=True):
        row["links"] = [dict(link)]
        row["weight"] = 1
    return parse_session_choice(raw)


def windowed(*windows, pool="pool-0", state="ok", pace=70, freshness="fresh"):
    """A pool reading whose windows carry their own pace evidence."""
    quota = reading(pool, state=state, pace=pace, freshness=freshness)
    quota.windows = [
        UsageWindowView(
            id=name,
            kind="other",
            label=name,
            percent_used=used,
            remaining_pct=None if days_left <= 0 else 100 - used,
            window_minutes=minutes,
            resets_at=NOW + days_left * 86400,
            state=window_state,
            past_reset=days_left <= 0,
        )
        for name, used, minutes, days_left, window_state in windows
    ]
    quota.read_at = NOW
    return quota


def period_request(**changes):
    return ChoiceRequest(OWNER, "host-a", **changes)


@pytest.mark.parametrize("same_agent", [False, True])
def test_deciding_period_exception_flips_the_balance_rank(same_agent):
    links = [dict(link) for link in SHARED_LINKS]
    if same_agent:
        links[1]["agent"] = links[0]["agent"]
    settings = shared_pool(*links)
    quotas = {"pool-0": windowed(("7d", 40, 10080, 4, "ok"), ("5h", 90, 300, 4, "low"))}
    assert pick(settings, readings=quotas).link.model == "model-0"
    for exception in ({("agent-0", "model-0"): "5h"}, {("agent-0", None): "5h"}):
        picked = pick(settings, readings=quotas, request=period_request(quota_periods=exception))
        if same_agent and ("agent-0", None) in exception:
            # An agent-wide exception affects both models equally.
            assert picked.link.model == "model-0"
        else:
            assert (picked.link.model, picked.pool) == ("model-1", "pool-0")
    overrides = {("agent-0", None): "5h", ("agent-0", "model-0"): "7d"}
    picked = pick(settings, readings=quotas, request=period_request(quota_periods=overrides))
    assert picked.link.model == "model-0"
    assert quotas["pool-0"].state == "ok"


def test_period_exception_only_moves_its_own_agent_and_model():
    settings = shared_pool(*SHARED_LINKS)
    quotas = {"pool-0": windowed(("7d", 40, 10080, 4, "ok"), ("5h", 90, 300, 4, "low"))}
    for exception in ({("agent-0", "model-9"): "5h"}, {("agent-8", "model-0"): "5h"}):
        picked = pick(settings, readings=quotas, request=period_request(quota_periods=exception))
        assert picked.link.agent_id == "agent-0"


@pytest.mark.parametrize("model_rule", ["old/x", None], ids=["renamed-model", "agent-only"])
def test_period_exception_survives_provider_rename_in_rank_and_warning(model_rule):
    from omnigent.models.model_reference import provider_groups

    first = {"agent": "agent-0", "model": "old/x", "meter": "pool-0"}
    quotas = {"pool-0": windowed(("7d", 40, 10080, 4, "ok"), ("5h", 90, 300, 4, "low"))}
    exceptions = {("agent-0", model_rule): "5h"}
    if model_rule is not None:
        exceptions[("agent-0", None)] = "7d"
        exceptions[("agent-0", "new/x")] = "7d"
    else:
        exceptions[("agent-0", "old/other")] = "7d"
    saved = dict(exceptions)
    request = period_request(
        models={"agent-0": frozenset({"new/x"})},
        provider_groups=provider_groups([("old", "new")]),
        quota_periods=exceptions,
    )
    settings = shared_pool(first, SHARED_LINKS[1])
    without_exception = replace(request, quota_periods={})
    assert pick(settings, readings=quotas, request=without_exception).link.model == "new/x"
    assert pick(settings, readings=quotas, request=request).link.agent_id == "agent-1"
    warning = pick(shared_pool(first), readings=quotas, request=request)
    assert warning.link.model == "new/x"
    assert warning.warnings == ("pool-0 may not last until its reset",)
    assert exceptions == saved


@pytest.mark.parametrize("route,pool", [("official", "codex-pro"), ("krill", "krill")])
def test_route_pin_candidate_matches_only_the_exact_model_period(route, pool):
    settings = shared_pool(
        {"agent": "agent-0", "model": f"{route}/gpt-large"},
        {"agent": "agent-1", "model": f"{route}/gpt-small"},
    )
    quotas = {pool: windowed(("7d", 40, 10080, 4, "ok"), ("5h", 90, 300, 4, "low"), pool=pool)}
    request = period_request(
        products={"agent-0": "codex", "agent-1": "codex"},
        models={"agent-0": frozenset({"gpt-large"}), "agent-1": frozenset({"gpt-small"})},
    )
    assert pick(settings, readings=quotas, request=request).link.model == f"{route}/gpt-large"
    for other_model in ("gpt-large", "other/gpt-large"):
        unmatched = replace(request, quota_periods={("agent-0", other_model): "5h"})
        assert (
            pick(settings, readings=quotas, request=unmatched).link.model == f"{route}/gpt-large"
        )
    demoted = replace(request, quota_periods={("agent-0", f"{route}/gpt-large"): "5h"})
    picked = pick(settings, readings=quotas, request=demoted)
    assert (picked.pool, picked.link.model) == (pool, f"{route}/gpt-small")


@pytest.mark.parametrize("period", ["5h", "codex-primary", "relay-primary"])
def test_period_exception_matches_native_labels_and_preserves_bucket_ids(db_uri, period):
    from omnigent.server.agent_quota import QuotaContext
    from tests.server.test_agent_quota import service_with_store, usage_report

    service = service_with_store(db_uri)
    service.report_account(OWNER, "codex-pro", usage_report(account_product="codex"), now=NOW)
    service.report(
        OWNER,
        "host-a",
        "codex",
        {
            "captured_at": NOW + 1,
            "limits": [
                {
                    "limit_id": bucket,
                    "windows": [
                        {
                            "kind": "primary",
                            "used_percent": used,
                            "window_duration_mins": 300,
                            "resets_at": NOW + 1 + 18000,
                        }
                    ],
                }
                for bucket, used in (("codex", 90), ("relay", 65))
            ],
        },
        now=NOW + 1,
    )
    quotas = service.pool_readings(QuotaContext(OWNER, "host-a"), now=NOW + 1)
    assert [(w.id, w.label) for w in quotas["codex-pro"].windows] == [
        ("codex-primary", "5h"),
        ("relay-primary", "5h"),
    ]
    # The other pool's tight reading beats low quota, but loses a weighted tie.
    quotas["other"] = reading("other", state="tight", pace=35)
    first = {"agent": "agent-0", "model": "official/gpt-large"}
    second = {"agent": "agent-1", "model": "model-1", "meter": "other"}
    request = period_request(
        products={"agent-0": "codex"},
        quota_periods={("agent-0", "official/gpt-large"): period},
    )
    picked = pick(shared_pool(first, second), readings=quotas, request=request, now=NOW + 1)
    assert picked.link.agent_id == ("agent-0" if period == "relay-primary" else "agent-1")
    warning = pick(shared_pool(first), readings=quotas, request=request, now=NOW + 1)
    assert warning.warnings == (
        () if period == "relay-primary" else ("codex-pro may not last until its reset",)
    )


def test_candidate_period_keeps_the_owners_custom_tiers():
    from omnigent.quota_pace import QuotaTiers

    settings = shared_pool(*SHARED_LINKS)
    quotas = {"pool-0": windowed(("7d", 60, 10080, 7, "tight"), state="tight", pace=40)}
    request = period_request(quota_periods={("agent-0", "model-0"): "7d"})
    assert pick(settings, readings=quotas, request=request).link.agent_id == "agent-0"
    custom = replace(request, quota_tiers=QuotaTiers(tight_below_pct=90, low_below_pct=45))
    assert pick(settings, readings=quotas, request=custom).link.agent_id == "agent-1"


def test_low_pace_tie_uses_each_candidates_deciding_window():
    settings = shared_pool(*SHARED_LINKS)
    low = windowed(("7d", 85, 10080, 7, "low"), ("5h", 90, 300, 4, "low"), state="low", pace=15)
    quotas = {"pool-0": low}
    assert pick(settings, readings=quotas).link.agent_id == "agent-0"
    exceptions = {("agent-0", "model-0"): "5h", ("agent-1", "model-1"): "7d"}
    request = period_request(quota_periods=exceptions)
    assert pick(settings, readings=quotas, request=request).link.agent_id == "agent-1"


def test_missing_window_and_stale_evidence_stay_fail_open():
    settings = shared_pool(*SHARED_LINKS)
    link = Link.model_validate({"agent": "agent-0", "model": "model-0", "meter": "pool-0"})
    quotas = {"pool-0": windowed(("7d", 40, 10080, 4, "ok"))}
    absent = period_request(quota_periods={("agent-0", "model-0"): "30d"})
    quota = candidate_quota(absent, link, quotas["pool-0"], NOW)
    assert quota is not None and (quota.state, quota.pace_pct) == ("unknown", None)
    assert pick(settings, readings=quotas, request=absent).link.agent_id == "agent-0"
    demote = period_request(quota_periods={("agent-0", "model-0"): "5h"})
    past_reset = windowed(("7d", 40, 10080, 4, "ok"), ("5h", 90, 300, -1, "low"))
    assert candidate_quota(demote, link, past_reset, NOW).state == "unknown"
    stale = windowed(("7d", 40, 10080, 4, "ok"), ("5h", 90, 300, 4, "low"), freshness="old")
    for readings in ({}, {"pool-0": stale}):
        assert pick(settings, readings=readings, request=demote).link.agent_id == "agent-0"


def test_candidate_period_never_bypasses_an_exhausted_pool_window():
    settings = shared_pool(*SHARED_LINKS)
    quotas = {"pool-0": windowed(("7d", 40, 10080, 4, "ok"), ("5h", 100, 300, 4, "exhausted"))}
    request = period_request(quota_periods={("agent-0", "model-0"): "7d"})
    held = pick(settings, readings=quotas, request=request)
    assert (held.outcome, held.reason, held.clears_at) == ("hold", "exhausted", NOW + 4 * 86400)
    assert [item.why for item in held.skipped] == ["exhausted", "exhausted"]


def test_warning_follows_the_picked_candidates_deciding_window():
    settings = shared_pool({"agent": "agent-0", "model": "model-0", "meter": "pool-0"})
    quotas = {"pool-0": windowed(("7d", 40, 10080, 4, "ok"), ("5h", 90, 300, 4, "low"))}
    assert pick(settings, readings=quotas).warnings == ()
    request = period_request(quota_periods={("agent-0", "model-0"): "5h"})
    assert pick(settings, readings=quotas, request=request).warnings == (
        "pool-0 may not last until its reset",
    )
