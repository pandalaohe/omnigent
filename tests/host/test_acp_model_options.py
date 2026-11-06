"""The host defaults preview reuses actual ACP advertisements."""

from unittest.mock import AsyncMock

import pytest
import yaml

from omnigent.host.frames import HostModelOptionsFrame
from omnigent.inner.acp_executor import AcpAgentConfig, AcpExecutor
from omnigent.onboarding.acp_auth import AcpAgentEntry
from tests.host.test_connect import _make_host_process


@pytest.mark.parametrize("lane", ["url", "command", "static", "agent"])
async def test_declared_sources_share_the_host_frame(tmp_path, monkeypatch, lane):
    from omnigent.models import model_catalog
    from omnigent.models.model_catalog import ModelEntry, ModelListing

    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / "data"))
    source = {
        "url": {"url": "http://example.test/v1"},
        "command": {"command": "printf 'model-a\\tModel A\\n'"},
        "static": {"static": ["model-a"]},
        "agent": {"agent": True},
    }[lane]
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "acp": {"agents": [{"name": "Neutral", "command": "neutral-acp-agent"}]},
                "model_sources": {"acp:neutral": source},
            }
        )
    )
    monkeypatch.setattr(
        model_catalog,
        "listing_for_provider",
        lambda *_args, **_kwargs: ModelListing(
            source="openai-compatible",
            verified=True,
            models=(ModelEntry(id="model-a", family="unknown"),),
            note="synthetic listing",
        ),
    )
    probe = AsyncMock(
        return_value={
            "models": [{"id": "model-a"}],
            "permission_options": [{"value": "review", "label": "Review"}],
            "error": None,
        }
    )
    monkeypatch.setattr("omnigent.inner.acp_harness.probe_acp_model_options", probe)
    host = _make_host_process()
    result = await host._handle_model_options(
        HostModelOptionsFrame(request_id="listing", harness="acp:neutral")
    )
    assert result.status == "ok", result.error
    assert result.request_id == "listing"
    assert [row["id"] for row in result.models] == ["model-a"]
    assert result.models[0]["source"]["kind"] == "model-source"
    assert result.permission_options == [{"value": "review", "label": "Review"}]
    probe.assert_awaited_once()
    probe.reset_mock()
    cached = await host._handle_model_options(
        HostModelOptionsFrame(request_id="cached", harness="acp:neutral")
    )
    assert cached.permission_options == result.permission_options
    probe.assert_not_awaited()


async def test_prewarm_skips_agent_processes(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / "data"))
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "model_sources": {
                    "acp:neutral": {"agent": True},
                    "copilot": {"command": "printf 'model-a\\n'"},
                }
            }
        )
    )
    probe = AsyncMock(side_effect=AssertionError("prewarm started an agent"))
    monkeypatch.setattr("omnigent.inner.acp_harness.probe_acp_model_options", probe)
    assert await _make_host_process()._prewarm_model_sources()
    probe.assert_not_awaited()


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
