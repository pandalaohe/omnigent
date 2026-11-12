"""Real OTLP speed intake, warning receipts and scoped reads."""

from copy import deepcopy

import httpx
import pytest
from fastapi import FastAPI
from google.protobuf.json_format import ParseDict
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import (
    ExportMetricsServiceRequest,
)

from omnigent.db.utils import now_epoch
from omnigent.server.routes.agent_quota import create_agent_quota_router
from omnigent.server.usage_evidence import UsageEvidenceService
from omnigent.stores.usage_evidence_store import UsageEvidenceStore
from omnigent.usage.otlp import normalize_export
from tests.server.routes.test_usage_otlp import OWNER, attribute, encode, receiver  # noqa: F401


def payload():
    end = now_epoch() // 3600 * 3600
    point = {
        "startTimeUnixNano": str((end - 3600) * 10**9),
        "timeUnixNano": str(end * 10**9),
        "attributes": [
            attribute("gen_ai.response.model", "model-a"),
            attribute("omnigent.usage.coverage", "complete"),
        ],
        "explicitBounds": [0.1, 1],
        "bucketCounts": [1, 2, 0],
        "count": "3",
    }
    return {
        "resourceMetrics": [
            {
                "resource": {
                    "attributes": [
                        attribute("service.name", "litellm"),
                        attribute("launch_mode", "omni"),
                        attribute("omnigent.usage.account", "a"),
                    ]
                },
                "scopeMetrics": [
                    {
                        "metrics": [
                            {
                                "name": "gen_ai.server.time_to_first_token",
                                "unit": "s",
                                "description": "valid samples",
                                "histogram": {"aggregationTemporality": 1, "dataPoints": [point]},
                            }
                        ]
                    }
                ],
            }
        ]
    }


def histogram(body):
    return body["resourceMetrics"][0]["scopeMetrics"][0]["metrics"][0]["histogram"]


@pytest.mark.parametrize("media", ["application/json", "application/x-protobuf"])
async def test_speed_intake_replay_warning_correction_and_read(request, db_uri, media):
    _, usage, hours = request.getfixturevalue("receiver")
    store = UsageEvidenceStore(db_uri)
    evidence = UsageEvidenceService(store)
    app = FastAPI()
    app.include_router(
        create_agent_quota_router(
            usage, auth_provider=None, host_store=None, evidence_service=evidence
        ),
        prefix="/v1",
    )
    body = payload()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:

        async def send():
            return await client.post(
                "/v1/me/usage/otlp/v1/metrics",
                content=encode(body, "metrics", media),
                headers={"Content-Type": media},
            )

        for _ in range(2):
            response = await send()
            assert response.status_code == 200
        [row] = (await client.get("/v1/me/usage/speed?range=h24")).json()["rows"]
        assert row["ttft"]["count"] == 3
        assert row["requests"] is None
        point = histogram(body)["dataPoints"][0]
        point.update(count="2", bucketCounts=[1, 1, 0])
        response = await send()
        if media == "application/x-protobuf":
            from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import (
                ExportMetricsServiceResponse,
            )

            partial = ExportMetricsServiceResponse.FromString(response.content).partial_success
            assert partial.rejected_data_points == 0 and "held" in partial.error_message
        else:
            assert "held" in response.json()["partialSuccess"]["errorMessage"]
            assert int(response.json()["partialSuccess"].get("rejectedDataPoints", 0)) == 0
        point["attributes"].append(
            {"key": "omnigent.usage.correction", "value": {"boolValue": True}}
        )
        assert (await send()).status_code == 200
        [row] = (await client.get("/v1/me/usage/speed")).json()["rows"]
        assert row["ttft"]["count"] == 2
        assert (
            await client.get("/v1/me/usage/speed?range=custom&from=2026-11-01&to=2026-11-12")
        ).status_code == 200
        for query in (
            "range=period",
            "tz=invalid",
            "by=invalid",
            "range=custom&from=2026-11-12&to=2026-11-01",
        ):
            assert (await client.get("/v1/me/usage/speed?" + query)).status_code == 422
        for end in (now_epoch() + 3600, now_epoch() - 401 * 86400):
            point["startTimeUnixNano"] = str((end - 3600) * 10**9)
            point["timeUnixNano"] = str(end * 10**9)
            response = await send()
            if media == "application/json":
                assert int(response.json()["partialSuccess"]["rejectedDataPoints"]) == 1
            else:
                assert (
                    ExportMetricsServiceResponse.FromString(
                        response.content
                    ).partial_success.rejected_data_points
                    == 1
                )
    assert hours.read(OWNER, 0, now_epoch()) == []
    usage.flush(force=True)
    assert not any(key.startswith("usage.account.") for key in usage.snapshot_store.read(OWNER))


@pytest.mark.parametrize(
    "change", ["count", "bounds", "nonfinite", "cumulative", "unit", "interval", "description"]
)
def test_malformed_speed_is_rejected_without_activity(change):
    body = payload()
    metric = body["resourceMetrics"][0]["scopeMetrics"][0]["metrics"][0]
    point = histogram(body)["dataPoints"][0]
    if change == "count":
        point["count"] = "4"
    elif change == "bounds":
        point["explicitBounds"] = [1, 0.1]
    elif change == "nonfinite":
        point["explicitBounds"] = ["Infinity", "Infinity"]
    elif change == "cumulative":
        histogram(body)["aggregationTemporality"] = 2
    elif change == "unit":
        metric["unit"] = "ms"
    elif change == "interval":
        point["startTimeUnixNano"] = point["timeUnixNano"]
    else:
        metric["description"] = "x" * 257
    result = normalize_export(ParseDict(body, ExportMetricsServiceRequest()), set(), set())
    assert result.rejected == 1
    assert not result.speed and not result.deltas and not result.rejected_accounts


def test_tpot_and_requests_need_no_histogram_sum():
    body = payload()
    metrics = body["resourceMetrics"][0]["scopeMetrics"][0]["metrics"]
    tpot = deepcopy(metrics[0])
    tpot["name"] = "gen_ai.server.time_per_output_token"
    requests = {
        "name": "omnigent.usage.speed.requests",
        "unit": "{request}",
        "sum": {
            "aggregationTemporality": 1,
            "isMonotonic": True,
            "dataPoints": [
                dict(
                    {
                        k: v
                        for k, v in histogram(body)["dataPoints"][0].items()
                        if k in {"startTimeUnixNano", "timeUnixNano", "attributes"}
                    },
                    asInt="5",
                )
            ],
        },
    }
    metrics.extend([tpot, requests])
    result = normalize_export(ParseDict(body, ExportMetricsServiceRequest()), set(), set())
    assert [p.metric for p in result.speed] == ["ttft", "tpot", "requests"]
    assert result.speed[-1].count == 5
    assert not result.deltas and result.rejected == 0
