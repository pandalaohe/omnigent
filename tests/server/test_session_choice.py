"""Session selection settings use one validated, revision-checked writer."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

import httpx
import pytest
from pydantic import ValidationError

from omnigent.server.session_choice import ChoiceRevisionConflict, parse_session_choice
from omnigent.server.user_preferences_store import (
    SqlAlchemyUserPreferencesStore,
    read_collab_settings,
)
from tests.server.test_user_preferences import _preferences_app

OWNER = "owner@example.test"


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


def test_revision_is_atomic_and_other_preferences_survive(db_uri):
    store = SqlAlchemyUserPreferencesStore(db_uri)
    store.patch_namespace(OWNER, "session_collab", {"childCap": 3})
    initial = store.session_choice(OWNER)

    def attempt(value):
        try:
            return save(store, value, initial["revision"])
        except ChoiceRevisionConflict:
            return "conflict"

    other = document()
    other["strategy"] = "balance"
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, [document(), other]))
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
