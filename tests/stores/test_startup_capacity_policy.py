"""Lower startup policy must be enforced by the shared reservation transaction."""

from uuid import uuid4

from omnigent.stores.session_startup_store import SessionStartupStore


def test_one_start_policy_survives_store_replacement(db_uri):
    first, replacement = SessionStartupStore(db_uri), SessionStartupStore(db_uri)
    host = uuid4().hex
    first.configure(host, host_resident_limit=7, deployment_resident_limit=20, startup_limit=1)
    for _ in range(2):
        first.enqueue(
            owner_user_id="owner",
            host_id=host,
            session_id=uuid4().hex,
            operation_key=uuid4().hex,
            input_id=None,
        )
    assert first.reserve(host) is not None
    assert replacement.reserve(host) is None
