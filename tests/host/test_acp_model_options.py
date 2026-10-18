"""The host defaults preview reuses actual ACP advertisements."""

import pytest

from omnigent.host.frames import HostModelOptionsFrame
from omnigent.inner.acp_executor import AcpAgentConfig, AcpExecutor
from omnigent.onboarding.acp_auth import AcpAgentEntry
from tests.host.test_connect import _make_host_process


@pytest.mark.asyncio
async def test_host_reads_last_advertised_model_efforts(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path))
    entry = AcpAgentEntry(slug="neutral", name="Neutral", command="neutral-acp-agent")
    monkeypatch.setattr("omnigent.onboarding.acp_auth.acp_agents", lambda config=None: [entry])
    executor = AcpExecutor(AcpAgentConfig(command=entry.command), cwd=str(tmp_path))
    host = _make_host_process()
    request = HostModelOptionsFrame(request_id="catalog", harness="acp:neutral")
    assert (await host._handle_model_options(request)).models == []
    executor._note_config_options(
        [
            {"id": "model", "currentValue": "model-a", "options": [{"value": "model-a"}]},
            {
                "id": "budget",
                "type": "select",
                "category": "thought_level",
                "currentValue": "deliberate",
                "options": [{"value": "deliberate"}],
            },
        ]
    )
    result = await host._handle_model_options(request)
    assert result.models[0]["efforts"] == ["deliberate"]
    executor._note_config_options(
        [
            {"id": "model", "currentValue": "model-a", "options": [{"value": "model-a"}]},
        ]
    )
    assert (await host._handle_model_options(request)).models[0]["efforts"] == []
