"""The commands copied from Connect must reach their real HTTP routes."""

import json
import re
import subprocess
import time
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from omnigent.server.agent_quota import AgentQuotaService
from omnigent.server.auth import RESERVED_USER_LOCAL
from omnigent.server.routes.agent_quota import create_agent_quota_router
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.usage_hours_store import UsageHoursStore
from omnigent.stores.usage_snapshot_store import UsageSnapshotStore


@pytest.fixture(scope="module")
def samples():
    module = Path(__file__).resolve().parents[2] / "web/src/pages/usageBoard/usageBoardConnect.ts"
    # Pure TypeScript module: no DOM or dependency resolution needed.
    script = """
      const m = await import(process.argv[1]);
      const sources = m.CONNECT_SOURCES.filter(s => s !== 'subscription');
      console.log(JSON.stringify(sources.map(source => {
        const command = m.buildConnectCommand('http://test', source);
        const payload = m.connectCommandPayload(command, Number(process.argv[2]));
        return {source, command, payload};
      })));
    """
    return json.loads(
        subprocess.check_output(
            [
                "node",
                "--experimental-strip-types",
                "--input-type=module",
                "-e",
                script,
                module.as_uri(),
                str(int(time.time())),
            ],
            text=True,
        )
    )


@pytest.mark.parametrize("source", ["otel", "vendor", "relay", "report", "manual", "undocumented"])
async def test_connect_sample_posts_to_route(db_uri, samples, source):
    sample = next(item for item in samples if item["source"] == source)
    hours = UsageHoursStore(db_uri)
    service = AgentQuotaService(
        SqlAlchemyConversationStore(db_uri),
        None,
        None,
        None,
        None,
        UsageSnapshotStore(db_uri),
        hours_store=hours,
    )
    app = FastAPI()
    app.include_router(
        create_agent_quota_router(service, auth_provider=None, host_store=None), prefix="/v1"
    )

    method, url = re.search(r'-X (\w+) "([^"]+)"', sample["command"]).groups()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.request(method, url, json=sample["payload"])
    assert response.status_code == 200, response.text
    if source == "otel":
        rows = hours.read(RESERVED_USER_LOCAL, 0, time.time() + 3600)
        assert len(rows) == 1
        assert rows[0].input_tokens == 100
    else:
        account = next(
            item
            for item in service.overview(RESERVED_USER_LOCAL).accounts
            if item.id == "my-gateway"
        )
        if source in {"manual", "undocumented", "relay"}:
            assert account.balances[0].remaining == (38 if source == "relay" else 25)
        else:
            assert account.windows[0].used_value == (12 if source == "vendor" else None)
