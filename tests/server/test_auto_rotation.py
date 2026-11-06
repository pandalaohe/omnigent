"""Auto-rotation settings, context rounds and label inheritance."""

from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest
from sqlalchemy.exc import SQLAlchemyError

from omnigent.entities import CompactionData, NewConversationItem
from omnigent.native._native_session_labels import replacement_session_labels
from omnigent.server.auth import LEVEL_OWNER, LEVEL_READ, RESERVED_USER_LOCAL
from omnigent.server.auto_rotation import (
    AUTO_ROTATION_LABEL_KEYS,
    AUTO_ROTATION_STATE_PREFIX,
    FRESH_ROUND_LABEL_KEY,
    INFERRED_COMPACTIONS_LABEL_KEY,
    NOTICED_COUNT_LABEL_KEY,
    auto_rotation_for,
    record_auto_rotation_usage,
)
from omnigent.server.routes._sessions.orchestration import _persist_external_session_usage
from omnigent.server.routes.sessions.routes_succession import _successor_labels
from omnigent.server.schemas import SessionEventInput
from omnigent.server.user_preferences_store import (
    SqlAlchemyUserPreferencesStore,
    _parse_collab_settings,
)
from omnigent.stores.conversation_store import ROTATE_REQUESTED_LABEL_KEY, SUCCEEDED_BY_LABEL_KEY
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from tests.server.routes.test_project_collaboration_routes import _build_app
from tests.server.test_user_preferences import _preferences_app


def _compact(store, sid):
    store.append(
        sid,
        [
            NewConversationItem(
                type="compaction",
                response_id="round",
                data=CompactionData(
                    summary="Summary",
                    last_item_id="a" * 32,
                    model="example/model",
                    token_count=10,
                ),
            )
        ],
    )


async def test_settings_round_trip(runtime_init, db_uri, tmp_path):
    app = _build_app(db_uri, tmp_path, preferences=True)
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(harness_override="codex-native")
    path = f"/v1/sessions/{conv.id}/auto-rotation"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        initial = await client.get(path)
        assert initial.status_code == 200
        body = initial.json()
        assert (body["enabled"], body["compactions_setting"], body["remaining_pct_setting"]) == (
            False,
            4,
            25,
        )
        assert set(body["sources"].values()) == {"default"}
        prefs = SqlAlchemyUserPreferencesStore(db_uri)
        prefs.patch_namespace(
            RESERVED_USER_LOCAL,
            "session_collab",
            {
                "autoRotationEnabled": True,
                "autoRotationCompactions": 6,
                "autoRotationRemainingPct": 30,
            },
        )
        inherited = (await client.get(path)).json()
        assert inherited["enabled"] and inherited["compactions_setting"] == 6
        assert set(inherited["sources"].values()) == {"user"}
        response = await client.patch(
            path, json={"enabled": "off", "compactions": 2, "remaining_pct": 90}
        )
        assert response.status_code == 200, response.text
        assert response.json()["compactions_setting"] == 2
        assert set(response.json()["sources"].values()) == {"session"}
        reset = await client.patch(path, json=dict.fromkeys(AUTO_ROTATION_LABEL_KEYS, "default"))
        assert set(reset.json()["sources"].values()) == {"user"}
        for invalid in (
            {"compactions": True},
            {"compactions": 21},
            {"remaining_pct": 0},
            {"enabled": True},
            {"unknown": 1},
            {"compactions": None},
            {"compactions": 1.0},
        ):
            response = await client.patch(path, json=invalid)
            assert response.status_code == 400, response.text
        assert (await client.get("/v1/sessions/missing/auto-rotation")).status_code == 404


@pytest.mark.parametrize(
    "harness", ["claude-native", "codex-native", "pi", "pi-native", "antigravity-native"]
)
async def test_eligibility(runtime_init, db_uri, tmp_path, harness):
    app = _build_app(db_uri, tmp_path)
    store = SqlAlchemyConversationStore(db_uri)
    parent = store.create_conversation(harness_override=harness)
    child = store.create_conversation(harness_override=harness, parent_conversation_id=parent.id)
    other = store.create_conversation(harness_override="claude-sdk")
    archived = store.create_conversation(harness_override=harness)
    store.update_conversation(archived.id, archived=True)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.get(f"/v1/sessions/{parent.id}/auto-rotation")).json()["eligible"]
        for conv, reason in ((child, "not_top_level"), (other, "harness"), (archived, "archived")):
            path = f"/v1/sessions/{conv.id}/auto-rotation"
            assert (await client.get(path)).json()["ineligible_reason"] == reason
            response = await client.patch(path, json={"enabled": "on"})
            assert response.status_code == 409
            assert response.json()["detail"]["code"] == "not_eligible"


async def test_settings_authorization_uses_owner_defaults(runtime_init, db_uri, tmp_path):
    store = SqlAlchemyConversationStore(db_uri)
    permissions = SqlAlchemyPermissionStore(db_uri)
    app = _preferences_app(db_uri, tmp_path, permission_store=permissions)
    conv = store.create_conversation(harness_override="codex-native")
    permissions.grant("alice@example.test", conv.id, LEVEL_OWNER)
    permissions.grant("bob@example.test", conv.id, LEVEL_READ)
    prefs = SqlAlchemyUserPreferencesStore(db_uri)
    prefs.patch_namespace("alice@example.test", "session_collab", {"autoRotationCompactions": 6})
    prefs.patch_namespace("bob@example.test", "session_collab", {"autoRotationCompactions": 2})
    path = f"/v1/sessions/{conv.id}/auto-rotation"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        viewer = {"X-Test-User": "bob@example.test"}
        response = await client.get(path, headers=viewer)
        assert response.status_code == 200, response.text
        assert response.json()["compactions_setting"] == 6
        assert (
            await client.patch(path, json={"enabled": "on"}, headers=viewer)
        ).status_code == 403
        stranger = await client.get(path, headers={"X-Test-User": "charlie@example.test"})
        assert stranger.status_code == 404, stranger.text


async def test_fresh_round_and_due_boundaries(runtime_init, db_uri):
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(harness_override="codex-native")
    store.set_labels(
        conv.id,
        {"omnigent.auto_rotation": "on", "omnigent.last_auto_compact_token_limit": "200000"},
    )
    state = SimpleNamespace(permission_store=None, user_preferences_store=None)

    async def usage(data):
        await _persist_external_session_usage(
            conv.id,
            SessionEventInput(type="external_session_usage", data=data),
            store,
            auto_rotation_app_state=state,
        )
        return auto_rotation_for(store.get_conversation(conv.id), store)

    for stamp in range(1_700_000_001, 1_700_000_005):
        with patch(
            "omnigent.stores.conversation_store.sqlalchemy_store.now_epoch", return_value=stamp
        ):
            _compact(store, conv.id)
    assert not (await usage({"context_tokens": 190000})).due
    assert (await usage({"context_tokens": 60000})).remaining_pct == 70
    assert store.get_conversation(conv.id).labels[FRESH_ROUND_LABEL_KEY] == "1700000004000000"
    assert not (await usage({"context_tokens": 148000})).due
    assert (await usage({"context_tokens": 150000})).due
    for key in (NOTICED_COUNT_LABEL_KEY, ROTATE_REQUESTED_LABEL_KEY, SUCCEEDED_BY_LABEL_KEY):
        store.set_labels(conv.id, {key: "1700000004000000"})
        assert not (await usage({"context_tokens": 160000})).due
        store.delete_label(conv.id, key)
    with patch(
        "omnigent.stores.conversation_store.sqlalchemy_store.now_epoch", return_value=1_700_000_005
    ):
        _compact(store, conv.id)
    assert not (await usage({"context_tokens": 190000})).due
    store.delete_label(conv.id, FRESH_ROUND_LABEL_KEY)
    store.set_labels(conv.id, {"omnigent.last_context_tokens": "60000"})
    for data in ({"context_window": 200000}, {"cumulative_cost_usd": 0.5}):
        await usage(data)
        assert FRESH_ROUND_LABEL_KEY not in store.get_conversation(conv.id).labels
    assert (await usage({"context_tokens": 60000})).compactions == 5
    assert (await usage({"context_tokens": 150000})).due


async def test_capped_count_still_observes_new_compaction_round(runtime_init, db_uri):
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(harness_override="codex-native")
    store.set_labels(
        conv.id,
        {
            "omnigent.auto_rotation": "on",
            "omnigent.last_auto_compact_token_limit": "200000",
            "omnigent.last_context_tokens": "150000",
        },
    )
    with patch(
        "omnigent.stores.conversation_store.sqlalchemy_store.now_epoch", return_value=1_700_000_000
    ):
        store.append(
            conv.id,
            [
                NewConversationItem(
                    type="compaction",
                    response_id=f"round-{i}",
                    data=CompactionData(
                        summary="Summary",
                        last_item_id="a" * 32,
                        model="example/model",
                        token_count=10,
                    ),
                )
                for i in range(1000)
            ],
        )
    store.set_labels(
        conv.id,
        {
            FRESH_ROUND_LABEL_KEY: "1700000000000000",
            NOTICED_COUNT_LABEL_KEY: "1700000000000000",
        },
    )
    with patch(
        "omnigent.stores.conversation_store.sqlalchemy_store.now_epoch", return_value=1_700_000_001
    ):
        _compact(store, conv.id)
    stats = store.get_compaction_stats(conv.id)
    assert stats.count == 1000
    assert stats.last_compaction_at == 1_700_000_001
    before = auto_rotation_for(store.get_conversation(conv.id), store)
    assert before.round_marker == 1_700_000_001_000_000
    assert not before.due
    state = SimpleNamespace(permission_store=None, user_preferences_store=None)
    for tokens in (60000, 150000):
        await _persist_external_session_usage(
            conv.id,
            SessionEventInput(
                type="external_session_usage",
                data={"context_tokens": tokens},
            ),
            store,
            auto_rotation_app_state=state,
        )
    labels = store.get_conversation(conv.id).labels
    assert labels[FRESH_ROUND_LABEL_KEY] == "1700000001000000"
    assert labels[NOTICED_COUNT_LABEL_KEY] == "1700000000000000"
    status = auto_rotation_for(store.get_conversation(conv.id), store)
    assert status.compactions == 1000 and status.due


async def test_no_compaction_round_marker_starts_at_zero(runtime_init, db_uri):
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(harness_override="codex-native")
    store.set_labels(
        conv.id,
        {"omnigent.auto_rotation": "on", "omnigent.last_auto_compact_token_limit": "200000"},
    )
    await _persist_external_session_usage(
        conv.id,
        SessionEventInput(
            type="external_session_usage",
            data={"context_tokens": 60000},
        ),
        store,
        auto_rotation_app_state=SimpleNamespace(
            permission_store=None, user_preferences_store=None
        ),
    )
    updated = store.get_conversation(conv.id)
    assert updated.labels[FRESH_ROUND_LABEL_KEY] == "0"
    status = auto_rotation_for(updated, store)
    assert status.round_marker == 0 and status.compactions == 0 and not status.due
    assert "round_marker" not in status.model_dump()


@pytest.mark.parametrize("enabled", [True, False])
async def test_antigravity_drop_count_and_switch_off(runtime_init, db_uri, enabled):
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(harness_override="antigravity-native")
    store.set_labels(
        conv.id,
        {
            "omnigent.auto_rotation": "on" if enabled else "off",
            "omnigent.last_context_window": "200000",
        },
    )
    state = SimpleNamespace(permission_store=None, user_preferences_store=None)
    for tokens in (180000, 60000, 80000, 30000):
        await _persist_external_session_usage(
            conv.id,
            SessionEventInput(type="external_session_usage", data={"context_tokens": tokens}),
            store,
            auto_rotation_app_state=state,
        )
    labels = store.get_conversation(conv.id).labels
    if enabled:
        assert labels[INFERRED_COMPACTIONS_LABEL_KEY] == "1"
        assert labels[FRESH_ROUND_LABEL_KEY] == "1"
    else:
        assert not any(key.startswith(AUTO_ROTATION_STATE_PREFIX) for key in labels)


async def test_admitted_usage_requests_share_current_round_not_their_old_snapshot(
    runtime_init, db_uri
):
    store = SqlAlchemyConversationStore(db_uri)
    source = store.create_conversation(harness_override="antigravity-native")
    store.set_labels(
        source.id,
        {
            "omnigent.auto_rotation": "on",
            "omnigent.last_context_window": "200000",
            "omnigent.last_context_tokens": "180000",
        },
    )
    admitted = store.get_conversation(source.id)
    state = SimpleNamespace(permission_store=None, user_preferences_store=None)
    for tokens in (60000, 30000):
        await _persist_external_session_usage(
            source.id,
            SessionEventInput(type="external_session_usage", data={"context_tokens": tokens}),
            store,
            conv=admitted,
            auto_rotation_app_state=state,
        )
    result = store.get_conversation(source.id)
    assert result.labels[INFERRED_COMPACTIONS_LABEL_KEY] == "1"
    assert result.labels[FRESH_ROUND_LABEL_KEY] == "1"
    assert result.labels["omnigent.last_context_tokens"] == "30000"


async def test_archive_committed_after_admission_prevents_round_state(runtime_init, db_uri):
    store = SqlAlchemyConversationStore(db_uri)
    source = store.create_conversation(harness_override="antigravity-native")
    store.set_labels(
        source.id,
        {
            "omnigent.auto_rotation": "on",
            "omnigent.last_context_window": "200000",
            "omnigent.last_context_tokens": "180000",
        },
    )
    admitted = store.get_conversation(source.id)
    store.update_conversation(source.id, archived=True)
    await _persist_external_session_usage(
        source.id,
        SessionEventInput(type="external_session_usage", data={"context_tokens": 60000}),
        store,
        conv=admitted,
        auto_rotation_app_state=SimpleNamespace(
            permission_store=None, user_preferences_store=None
        ),
    )
    result = store.get_conversation(source.id)
    assert result.archived
    assert result.labels["omnigent.last_context_tokens"] == "60000"
    assert not any(key.startswith(AUTO_ROTATION_STATE_PREFIX) for key in result.labels)


@pytest.mark.parametrize("enabled", ["on", "off"])
def test_failed_eligibility_read_still_commits_usage(db_uri, monkeypatch, enabled):
    store = SqlAlchemyConversationStore(db_uri)
    source = store.create_conversation(harness_override="antigravity-native")
    store.set_labels(
        source.id,
        {
            "omnigent.auto_rotation": enabled,
            "omnigent.last_context_window": "200000",
            "omnigent.last_context_tokens": "180000",
            INFERRED_COMPACTIONS_LABEL_KEY: "4",
            FRESH_ROUND_LABEL_KEY: "4",
        },
    )
    admitted = store.get_conversation(source.id)
    read = store.get_conversation
    calls = 0

    def unavailable_once(session_id):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise SQLAlchemyError("eligibility read unavailable")
        return read(session_id)

    monkeypatch.setattr(store, "get_conversation", unavailable_once)
    result_status = record_auto_rotation_usage(
        source.id, {"omnigent.last_context_tokens": "190000"}, False, store, conv=admitted
    )
    result = read(source.id)
    assert result_status is None
    assert result.labels["omnigent.last_context_tokens"] == "190000"
    assert result.labels[INFERRED_COMPACTIONS_LABEL_KEY] == "4"
    assert result.labels[FRESH_ROUND_LABEL_KEY] == "4"


@pytest.mark.parametrize(
    "harness,expected",
    [
        ("pi", ["usage", "compaction_point", "compactions"]),
        ("pi-native", ["compaction_point"]),
        ("antigravity-native", ["compaction_point", "compactions"]),
        ("codex-native", ["compaction_point"]),
    ],
)
def test_approximation_and_unknown_usage(db_uri, harness, expected):
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(harness_override=harness)
    assert auto_rotation_for(conv, store).remaining_pct is None
    store.set_labels(
        conv.id,
        {"omnigent.last_context_window": "200000", "omnigent.last_context_tokens": "91808"},
    )
    status = auto_rotation_for(store.get_conversation(conv.id), store)
    assert status.approximate == expected
    if harness in {"pi", "pi-native"}:
        assert status.remaining_pct == 50
    store.set_labels(
        conv.id,
        {"omnigent.last_auto_compact_token_limit": "200000", "omnigent.import.source": "example"},
    )
    status = auto_rotation_for(store.get_conversation(conv.id), store)
    assert "compactions" in status.approximate
    if harness == "codex-native":
        assert status.approximate == ["compactions"]


def test_stats_and_fork_successor_labels(db_uri):
    store = SqlAlchemyConversationStore(db_uri)
    source = store.create_conversation(harness_override="codex-native")
    assert store.get_compaction_stats(source.id).count == 0
    settings = dict(zip(AUTO_ROTATION_LABEL_KEYS.values(), ("on", "4", "25"), strict=True))
    store.set_labels(
        source.id,
        {
            **settings,
            FRESH_ROUND_LABEL_KEY: "2",
            NOTICED_COUNT_LABEL_KEY: "2",
            INFERRED_COMPACTIONS_LABEL_KEY: "2",
        },
    )
    _compact(store, source.id)
    _compact(store, source.id)
    source = store.get_conversation(source.id)
    assert store.get_compaction_stats(source.id).count == 2
    assert (
        store.get_compaction_stats(source.id).last_compaction_at
        == store.list_items(source.id, type="compaction", order="desc", limit=1).data[0].created_at
    )
    fork = store.fork_conversation(source.id)
    assert store.get_compaction_stats(fork.id).count == 2
    for labels in [
        replacement_session_labels(source.labels),
        fork.labels,
        *[
            _successor_labels(source, harness, None)
            for harness in (
                "pi",
                "pi-native",
                "claude-native",
                "codex-native",
                "antigravity-native",
            )
        ],
    ]:
        assert all(labels[key] == value for key, value in settings.items())
        assert not any(key.startswith(AUTO_ROTATION_STATE_PREFIX) for key in labels)


def test_malformed_preferences_and_failed_reads_do_not_trigger(db_uri, monkeypatch):
    parsed = _parse_collab_settings(
        {
            "autoRotationEnabled": "on",
            "autoRotationCompactions": True,
            "autoRotationRemainingPct": 91,
        }
    )
    assert (
        parsed.auto_rotation_enabled,
        parsed.auto_rotation_compactions,
        parsed.auto_rotation_remaining_pct,
    ) == (False, 4, 25)
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(harness_override="codex-native")
    store.set_labels(
        conv.id,
        {
            "omnigent.auto_rotation": "on",
            "omnigent.auto_rotation.compactions": "bad",
            "omnigent.last_context_tokens": "190000",
            "omnigent.last_auto_compact_token_limit": "200000",
            FRESH_ROUND_LABEL_KEY: "4",
        },
    )
    for _ in range(4):
        _compact(store, conv.id)
    conv = store.get_conversation(conv.id)
    assert auto_rotation_for(conv, store).compactions_setting == 4
    prefs = SqlAlchemyUserPreferencesStore(db_uri)

    def failed(*args):
        raise SQLAlchemyError("unavailable")

    monkeypatch.setattr(prefs, "get", failed)
    assert not auto_rotation_for(conv, store, prefs_store=prefs).due
    monkeypatch.setattr(store, "get_compaction_stats", failed)
    assert not auto_rotation_for(conv, store).due
