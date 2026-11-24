"""Provider refusal state is shared by quota display and session admission."""

import pytest

from omnigent.server.session_choice import ChoiceState, TileLimits, tile_line
from omnigent.usage.contract import UsageAccountView


def test_refusal_wins_over_balance_without_reset_and_is_owner_scoped():
    from omnigent.server.session_choice import ProviderRefusal

    state = ChoiceState()
    state.refusals[("owner", "account", "route/model")] = ProviderRefusal(
        "billing_disabled", 100, None, "session", "turn"
    )
    readings = {
        "account": UsageAccountView(
            id="account",
            kind="account",
            display_name="Account",
            state="ok",
            freshness="fresh",
            balance_freshness="fresh",
            renews_at=1000,
            source_type="relay",
            source="sample-feeder",
            source_at=200,
            balances=[{"id": "balance", "unit": "USD", "remaining": 10}],
        )
    }
    effective = state.quota_readings("owner", readings, 101)
    line = tile_line(TileLimits(), effective["account"], None, 101)
    assert line.state == "stop"
    assert line.reason == "billing_disabled"
    assert line.clears_at is None
    assert line.source == "session:turn"
    assert readings["account"].state == "ok"
    assert effective["account"].balances[0].remaining == 10
    assert effective["account"].renews_at == 1000
    assert effective["account"].source == "sample-feeder"
    assert state.quota_readings("other", readings, 101) == readings
    assert state.quota_readings("owner", readings, 102)["account"].state == "stopped"


@pytest.mark.asyncio
@pytest.mark.parametrize("mapping_changes", [False, True])
async def test_record_and_recover_requires_owner_route_account_and_new_turn(
    monkeypatch, mapping_changes
):
    import json
    from types import SimpleNamespace
    from unittest.mock import Mock

    from omnigent.server.child_cap import ChildCapLocks
    from omnigent.server.session_choice import (
        CHOICE_LABEL,
        parse_session_choice,
        provider_route,
        state_for,
    )
    from omnigent.server.session_choice_runtime import (
        record_provider_recovery,
        record_quota_exhaustion,
    )

    # Receipts take the owner's admission lock, so the app needs the store and the lock map.
    app = SimpleNamespace(
        state=SimpleNamespace(
            conversation_store=Mock(),
            permission_store=None,
            child_cap_locks=ChildCapLocks(),
        )
    )
    session = SimpleNamespace(id="session", labels={CHOICE_LABEL: json.dumps({"pool": "account"})})
    identity = SimpleNamespace(host="host", agent="agent", model="route/model", product="codex")
    original = parse_session_choice(
        {"codexRoutePools": {"route": "account", "route-alias": "account"}}
        if mapping_changes
        else {}
    )
    replacement = parse_session_choice({"codexRoutePools": {"route": "other-account"}})
    settings = original
    monkeypatch.setattr(
        "omnigent.server.session_choice_runtime.choice_settings", lambda *_: settings
    )
    monkeypatch.setattr(
        "omnigent.server.session_collab.collab_owner_for", Mock(return_value="owner")
    )
    monkeypatch.setattr(
        "omnigent.server.session_choice_runtime.session_identity", lambda *_: identity
    )
    state = state_for(app)
    await record_provider_recovery(
        app,
        session,
        from_runner=True,
        input_generation=1,
        response_id="older-success",
        started=True,
    )
    args = {
        "actor": "other",
        "from_runner": False,
        "response_id": "failure",
        "input_generation": 2,
    }
    message = "HTTP 403 balance billing is disabled for this key"
    await record_quota_exhaustion(app, session, message, **args)
    assert not state.refusals
    args.update(actor="owner", from_runner=True)
    await record_provider_recovery(
        app, session, from_runner=True, response_id="failure", input_generation=2, started=True
    )
    if mapping_changes:
        settings = replacement
        identity.model = "route-alias/model"
    await record_quota_exhaustion(app, session, message, **args)
    key = (
        "owner",
        "account",
        provider_route(
            "host", "agent", "route/model", route_pools=original.codex_route_pools, product="codex"
        ),
    )
    assert state.refusals[key].reason == "billing_disabled"
    other_key = ("owner", "other-account", key[2])
    state.refusals[other_key] = state.refusals[key]
    await record_provider_recovery(
        app, session, from_runner=True, input_generation=1, response_id="older-success"
    )
    assert key in state.refusals
    for runner, turn, model in [
        (False, "success", "route/model"),
        (True, "failure", "route/model"),
        (True, "success", "other/model"),
    ]:
        identity.model = model
        await record_provider_recovery(
            app, session, from_runner=runner, input_generation=3, response_id=turn
        )
        assert key in state.refusals
    identity.model = "route/another-model"
    await record_provider_recovery(
        app, session, from_runner=True, input_generation=3, response_id="success"
    )
    assert key in state.refusals
    settings = original
    await record_provider_recovery(
        app, session, from_runner=True, input_generation=3, response_id="success", started=True
    )
    if mapping_changes:
        settings = replacement
        identity.model = "route-alias/another-model"
    await record_provider_recovery(
        app, session, from_runner=True, input_generation=3, response_id="success"
    )
    assert key not in state.refusals
    settings = original
    identity.model = "route/another-model"

    # Routine successful turns replace correlation, rather than retaining every turn.
    for generation in range(4, 104):
        await record_provider_recovery(
            app,
            session,
            from_runner=True,
            input_generation=generation,
            response_id=f"success-{generation}",
            started=True,
        )
        await record_provider_recovery(
            app,
            session,
            from_runner=True,
            input_generation=generation,
            response_id=f"success-{generation}",
        )
    assert len(state.provider_attempts) == 1
    latest = dict(state.provider_attempts)
    for generation, turn in [(1, "older-success"), (103, "success-103")]:
        await record_provider_recovery(
            app,
            session,
            from_runner=True,
            input_generation=generation,
            response_id=turn,
            started=True,
        )
        await record_quota_exhaustion(
            app,
            session,
            message,
            actor="owner",
            from_runner=True,
            response_id=turn,
            input_generation=generation,
        )
        assert key not in state.refusals
    assert state.provider_attempts == latest
    assert other_key in state.refusals
    # First delivery of an older failure must not undo the later recovery.
    await record_quota_exhaustion(
        app,
        session,
        message,
        actor="owner",
        from_runner=True,
        response_id="older-success",
        input_generation=1,
    )
    assert key not in state.refusals


def test_refusal_is_scoped_for_candidates_but_shared_with_tile():
    from omnigent.server.session_choice import (
        ChoiceRequest,
        Link,
        ProviderRefusal,
        SessionChoice,
        candidate_lines,
        provider_route,
    )

    state = ChoiceState()
    state.refusals[("owner", "account", provider_route("host", "agent", "route/model"))] = (
        ProviderRefusal("billing_disabled", 100, 110, "session", "failure")
    )
    readings = state.quota_readings("owner", {}, 101)
    request = ChoiceRequest("owner", "host")
    for route, expected in [("route", "stop"), ("other", "unknown")]:
        link = Link(agent="agent", model=f"{route}/model", meter="account")
        assert (
            candidate_lines(SessionChoice(), link, request, readings, {}, 101)[0][1].state
            == expected
        )
    assert state.quota_readings("owner", {}, 111) == {}


@pytest.mark.parametrize(
    "refused_model,candidate_model,expected",
    [
        ("model", "model", "stop"),
        ("route-primary/model", "model", "unknown"),
        ("route-primary/model", "route-primary/model", "stop"),
        ("model", "route-primary/model", "unknown"),
    ],
)
def test_codex_meter_keeps_refusal_on_the_verbatim_route(refused_model, candidate_model, expected):
    from omnigent.server.session_choice import (
        ChoiceRequest,
        Link,
        ProviderRefusal,
        SessionChoice,
        candidate_lines,
        provider_route,
    )

    state = ChoiceState()
    state.refusals[("owner", "pool-primary", provider_route("host", "agent", refused_model))] = (
        ProviderRefusal("billing_disabled", 100, None, "session", "turn")
    )
    readings = state.quota_readings("owner", {}, 101)
    assert readings["pool-primary"].state == "stopped"
    request = ChoiceRequest("owner", "host", products={"agent": "codex"})
    link = Link(agent="agent", model=candidate_model, meter="pool-primary")
    assert (
        candidate_lines(SessionChoice(), link, request, readings, {}, 101)[0][1].state == expected
    )
    assert link.model == candidate_model


@pytest.mark.parametrize("product,expected", [("codex", True), ("pi", False)])
def test_route_aliases_share_provider_refusal_only_for_codex(product, expected):
    from omnigent.server.session_choice import parse_session_choice, provider_route

    settings = parse_session_choice(
        {"codexRoutePools": {"route-a": "pool-a", "route-b": "pool-a"}}
    )
    first = provider_route(
        "host", "agent", "route-a/model", route_pools=settings.codex_route_pools, product=product
    )
    second = provider_route(
        "host", "agent", "route-b/model", route_pools=settings.codex_route_pools, product=product
    )
    assert (first == second) is expected
