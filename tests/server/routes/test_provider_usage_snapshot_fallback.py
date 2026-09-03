"""Legacy provider usage remains readable when labels live in a split database."""

from __future__ import annotations

import json
from pathlib import Path

from omnigent.server.routes._sessions.orchestration import _build_session_response
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore

_LABEL_KEY = "omnigent.last_provider_usage_limits"


def test_split_db_snapshot_repairs_the_observed_clipped_legacy_label(
    tmp_path: Path,
) -> None:
    """GET compatibility does not depend on Alembic reaching the labels database."""
    split_db_conversation_store = SqlAlchemyConversationStore(
        f"sqlite:///{tmp_path / 'omnigent.db'}",
        f"sqlite:///{tmp_path / 'conversations.db'}",
    )
    limits = {
        "provider": "Claude",
        "captured_at": 1_788_312_443,
        "windows": [
            {
                "label": "5h",
                "aria_label": "5 hour",
                "used_percent": 3.0,
                "duration_mins": 300,
                "resets_at": 1_788_324_000,
            },
            {
                "label": "w",
                "aria_label": "weekly",
                "used_percent": 1.0,
                "duration_mins": 10_080,
                "resets_at": 1_788_627_600,
            },
        ],
    }
    encoded = json.dumps(limits, separators=(",", ":"))
    assert len(encoded) == 257

    created = split_db_conversation_store.create_conversation(
        agent_id="87fd3a160be54a0590e0b7fd46a72fa1"
    )
    split_db_conversation_store.set_labels(created.id, {_LABEL_KEY: encoded[:256]})
    reloaded = split_db_conversation_store.get_conversation(created.id)
    assert reloaded is not None
    assert reloaded.provider_usage_limits is None

    response = _build_session_response(reloaded, [], "idle")

    assert response.provider_usage_limits == limits
