"""Existing Stop/interrupt controls fence admission before clearing saved inputs."""

import pytest

from tests.server.integration.test_startup_queue_wiring import (
    _HOST_ID,
    _multipart_create,
)
from tests.server.integration.test_startup_queue_wiring import (
    app as app,
)
from tests.server.integration.test_startup_queue_wiring import (
    conv_store as conv_store,
)
from tests.server.integration.test_startup_queue_wiring import (
    enable_admission as enable_admission,
)
from tests.server.integration.test_startup_queue_wiring import (
    register_host as register_host,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("claimed", [False, True])
@pytest.mark.parametrize("control", ["interrupt", "stop_session"])
async def test_interrupt_fences_startup_before_cancelling_canonical_input(
    app,
    client,
    conv_store,
    register_host,
    claimed,
    control,
):
    register_host()
    response = await _multipart_create(
        client,
        {
            "host_id": _HOST_ID,
            "workspace": "/Users/sample/project",
        },
    )
    assert response.status_code == 201, response.text
    session_id = response.json()["session_id"]
    startup = app.state.startup_store.for_session(session_id)
    if claimed:
        attempt = app.state.startup_store.reserve(_HOST_ID)
        assert app.state.startup_store.claim_boundary(
            startup.id,
            attempt.attempt_id,
            runtime_id="context",
            host_generation=app.state.host_registry.get(_HOST_ID).startup_generation,
            boundary="runner_prepare",
            claim_id="permission",
        )
    result = await client.post(f"/v1/sessions/{session_id}/events", json={"type": control})
    assert result.status_code == (503 if claimed and control == "interrupt" else 202), result.text
    current = app.state.startup_store.for_session(session_id)
    assert current.state == ("cancelling" if claimed else "cancelled")
    assert not app.state.startup_store.claim(startup.id, current.attempt_id, runtime_id="late")
    assert conv_store.list_pending_inputs(session_id) == []
    later = await client.post(
        f"/v1/sessions/{session_id}/events",
        json={
            "type": "message",
            "data": {"role": "user", "content": []},
        },
    )
    assert later.status_code == 409, later.text
    assert conv_store.list_pending_inputs(session_id) == []
