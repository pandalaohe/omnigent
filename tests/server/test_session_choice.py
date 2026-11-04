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
    choose,
    floor_off_active,
    holds_slot,
    model_family,
    parse_session_choice,
    pool_of_session,
)
from omnigent.server.user_preferences_store import (
    SqlAlchemyUserPreferencesStore,
    read_collab_settings,
)
from omnigent.usage.contract import UsageAccountView, UsageWindowView
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
        running or {},
        floors or {},
        {},
        counters if counters is not None else {},
        now=now,
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
    assert skipped.skipped[0].until == pytest.approx(NOW)
    # At equality the pool is held; a second later it has a little usable room.
    assert pick(settings, readings={"pool-0": quota}, now=NOW + 1).pool == "pool-0"
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
    assert pick(settings, request=unavailable).pool == "pool-1"
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
