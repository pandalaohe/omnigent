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
async def test_record_and_recover_requires_owner_route_account_and_new_turn(monkeypatch):
    import json
    from types import SimpleNamespace
    from unittest.mock import Mock

    from omnigent.server.session_choice import CHOICE_LABEL, provider_route, state_for
    from omnigent.server.session_choice_runtime import (
        record_provider_recovery,
        record_quota_exhaustion,
    )

    app = SimpleNamespace(state=SimpleNamespace(conversation_store=None, permission_store=None))
    session = SimpleNamespace(id="session", labels={CHOICE_LABEL: json.dumps({"pool": "account"})})
    identity = SimpleNamespace(host="host", agent="agent", model="route/model")
    monkeypatch.setattr(
        "omnigent.server.session_collab.collab_owner_for", Mock(return_value="owner")
    )
    monkeypatch.setattr(
        "omnigent.server.session_choice_runtime.session_identity", lambda *_: identity
    )
    state = state_for(app)
    await record_provider_recovery(
        app, session, from_runner=True, response_id="older-success", started=True
    )
    args = {"actor": "other", "from_runner": False, "response_id": "failure"}
    message = "HTTP 403 balance billing is disabled for this key"
    await record_quota_exhaustion(app, session, message, **args)
    assert not state.refusals
    args.update(actor="owner", from_runner=True)
    await record_quota_exhaustion(app, session, message, **args)
    key = ("owner", "account", provider_route("host", "agent", "route/model"))
    assert state.refusals[key].reason == "billing_disabled"
    other_key = ("owner", "other-account", key[2])
    state.refusals[other_key] = state.refusals[key]
    await record_provider_recovery(app, session, from_runner=True, response_id="older-success")
    assert key in state.refusals
    for runner, turn, model in [
        (False, "success", "route/model"),
        (True, "failure", "route/model"),
        (True, "success", "other/model"),
    ]:
        identity.model = model
        await record_provider_recovery(app, session, from_runner=runner, response_id=turn)
        assert key in state.refusals
    identity.model = "route/another-model"
    await record_provider_recovery(app, session, from_runner=True, response_id="success")
    assert key in state.refusals
    await record_provider_recovery(
        app, session, from_runner=True, response_id="success", started=True
    )
    await record_provider_recovery(app, session, from_runner=True, response_id="success")
    assert key not in state.refusals
    assert other_key in state.refusals


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


def test_plain_codex_meter_candidate_matches_its_pinned_route():
    from omnigent.server.session_choice import (
        ChoiceRequest,
        Link,
        ProviderRefusal,
        SessionChoice,
        candidate_lines,
        provider_route,
    )

    state = ChoiceState()
    state.refusals[("owner", "codex-pro", provider_route("host", "agent", "official/model"))] = (
        ProviderRefusal("billing_disabled", 100, None, "session", "turn")
    )
    readings = state.quota_readings("owner", {}, 101)
    request = ChoiceRequest("owner", "host", products={"agent": "codex"})
    link = Link(agent="agent", model="model", meter="codex-pro")
    assert candidate_lines(SessionChoice(), link, request, readings, {}, 101)[0][1].state == "stop"
