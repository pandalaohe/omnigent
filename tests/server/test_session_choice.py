"""Session selection settings use one validated, revision-checked writer."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace

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
    choice_document,
    choice_revision,
    choose,
    floor_off_active,
    holds_slot,
    model_family,
    parse_session_choice,
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
    from omnigent.server.routes.session_choice import create_session_choice_router
    from tests.server.test_user_preferences import _HeaderAuthProvider

    store = SqlAlchemyUserPreferencesStore(db_uri)
    before, target, proof, plan = conversion_fixture(store)
    calls = []

    def fresh(owner, stored):
        calls.append(owner)
        return plan

    app = FastAPI()
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
def test_codex_ability_cap_uses_unpinned_model_identity(caller_model):
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
    assert choice.link.model == "official/gpt-small"
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
    request = ChoiceRequest(
        OWNER,
        "host-a",
        products={"agent-0": "codex", "agent-1": "codex"},
        models={agent: frozenset({"gpt-example"}) for agent in ("agent-0", "agent-1")},
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


@pytest.mark.parametrize("route,pool", [("official", "codex-pro"), ("krill", "krill")])
def test_explicit_codex_route_meter_pins_pick_and_hold(route, pool):
    raw = table((1,)).model_dump(mode="json", by_alias=True)
    raw["hosts"]["host-a"]["work"][0][0]["links"][0] = {
        "agent": "agent-0",
        "model": "gpt-example",
        "meter": pool,
    }
    raw["limits"] = {pool: {"maxRunning": 1}}
    settings = parse_session_choice(raw)
    request = ChoiceRequest(OWNER, "host-a", products={"agent-0": "codex"})
    for running, outcome in (({}, "pick"), ({pool: ("child",)}, "hold")):
        choice = pick(settings, request=request, running=running)
        assert (choice.outcome, choice.pool, choice.link.model) == (
            outcome,
            pool,
            f"{route}/gpt-example",
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
    ],
)
def test_unique_model_substitution_is_not_limited_to_relay_routes(product, model):
    # A unique identical model suffix can substitute even without a provider alias.
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
    assert result.link.model == "gpt-example"
    assert result.notes[0]["outcome"] == "substituted"
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
    assert any("trying it as unknown" in warning for warning in result.warnings)


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
