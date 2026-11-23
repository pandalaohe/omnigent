"""Runner-side argument handling and error mapping for the project tools."""

import asyncio
import json

import httpx
import pytest

from omnigent.runner import session_projects
from omnigent.runner.identity import ORCHESTRATION_SOURCE_HEADER
from omnigent.runner.session_projects import execute_project_list, execute_session_update

pytestmark = pytest.mark.asyncio

SOURCE = "a" * 32
FAILING = "b" * 32
FAMILY_MEMBER = "c" * 32
SUB_AGENT = "d" * 32
PARENT = "e" * 32
PROJECT_ID = "f" * 32
READ_SHARED = "0" * 32
MISSING = "1" * 31 + "2"
CHECKED_INFO = {"session_move_source_checks": True, "session_settings_source_checks": True}


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://server")


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ({"project": "Q3"}, "session_id must be"),
        ({"session_id": 7, "project": "Q3"}, "session_id must be"),
        ({"session_id": SOURCE}, "nothing to update"),
        ({"session_id": SOURCE, "project": None}, "project must be a name"),
        ({"session_id": SOURCE, "project": "x" * 101}, "project must be a name"),
        ({"session_id": SOURCE, "project": "  "}, "project must be a name"),
        ({"session_id": "not-a-session", "project": "Q3"}, "invalid session ID"),
    ],
)
async def test_update_rejects_invalid_arguments_before_any_request(args, message):
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(500)

    async with _client(handler) as client:
        result = json.loads(await execute_session_update(args, client, SOURCE))
    assert message in result["error"]
    assert requests == []


@pytest.mark.parametrize("info", [{}, {"session_move_source_checks": False}])
async def test_update_refuses_a_server_without_source_checks_before_any_write(info):
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=info)

    async with _client(handler) as client:
        result = json.loads(
            await execute_session_update({"session_id": SOURCE, "project": "Q3"}, client, SOURCE)
        )
    assert "cannot check the calling session's sharing" in result["error"]
    assert [(r.method, r.url.path) for r in requests] == [("GET", "/v1/info")]


@pytest.mark.parametrize(
    ("reply", "reason"),
    [
        (httpx.ReadTimeout(""), "ReadTimeout"),
        (httpx.Response(404), "HTTP 404"),
        (httpx.Response(503), "HTTP 503"),
        (httpx.Response(200, text="not JSON"), "invalid capability response"),
        (httpx.Response(200, json=[]), "invalid capability response"),
    ],
)
async def test_update_reports_why_the_capability_probe_failed(reply, reason):
    def handler(request: httpx.Request) -> httpx.Response:
        assert (request.method, request.url.path) == ("GET", "/v1/info")
        if isinstance(reply, Exception):
            raise reply
        return reply

    async with _client(handler) as client:
        result = json.loads(
            await execute_session_update(
                {"session_id": SUB_AGENT, "model": "host-model"}, client, SOURCE
            )
        )
    assert "GET /v1/info" in result["error"]
    assert reason in result["error"]
    assert "upgrade" not in result["error"]


def _session_server(requests: list[httpx.Request]):
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = request.url.path
        if path == "/v1/info":
            return httpx.Response(200, json=CHECKED_INFO)
        if path == "/v1/projects":
            return httpx.Response(200, json={"data": [{"id": PROJECT_ID, "name": "Q3"}]})
        if request.method == "GET" and path.endswith(FAILING):
            raise httpx.ConnectError("connection reset")
        if request.method == "GET" and path.endswith(SUB_AGENT):
            return httpx.Response(200, json={"id": SUB_AGENT, "parent_session_id": PARENT})
        if request.method == "GET" and path.endswith(FAMILY_MEMBER):
            family = {"id": FAMILY_MEMBER, "family": {"representative_id": PARENT}}
            return httpx.Response(200, json=family)
        if request.method == "GET" and path.endswith(READ_SHARED):
            # Already in the target project, so only the owner check keeps it from "unchanged".
            shared = {"id": READ_SHARED, "project_id": PROJECT_ID, "permission_level": 1}
            return httpx.Response(200, json=shared)
        if request.method == "GET" and path.endswith(MISSING):
            return httpx.Response(404, json={"detail": "Session not found"})
        if request.method == "GET":
            return httpx.Response(200, json={"id": path.rsplit("/", 1)[-1], "labels": {}})
        return httpx.Response(200, json={})

    return handler


@pytest.mark.parametrize(
    ("session_id", "error"),
    [
        (FAILING, "session could not be read: connection reset"),
        (SUB_AGENT, f"sub-agent sessions follow their parent's project; update {PARENT} instead"),
        (FAMILY_MEMBER, f"this session shares its family's project; update {PARENT} instead"),
        (READ_SHARED, "only the session's owner can change its project"),
        (MISSING, "session not found or not visible to you (Session not found)"),
    ],
)
async def test_update_refuses_a_session_it_cannot_refile_without_any_write(session_id, error):
    requests: list[httpx.Request] = []
    async with _client(_session_server(requests)) as client:
        result = json.loads(
            await execute_session_update(
                {"session_id": session_id, "project": "New"}, client, SOURCE
            )
        )
    assert result == {"session_id": session_id, "error": error}
    assert not [r for r in requests if r.url.path == "/v1/projects" or r.method != "GET"]


async def test_update_files_the_session_and_sends_the_source_header():
    requests: list[httpx.Request] = []
    hyphenated_source = "AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA"
    async with _client(_session_server(requests)) as client:
        result = json.loads(
            await execute_session_update(
                {"session_id": hyphenated_source, "project": " Q3 "}, client, SOURCE
            )
        )
    assert result == {
        "session_id": hyphenated_source,
        "status": "updated",
        "project": "Q3",
        "project_created": False,
    }
    (patched,) = [r for r in requests if r.method == "PATCH"]
    assert patched.url.path == f"/v1/sessions/{SOURCE}"
    assert patched.headers[ORCHESTRATION_SOURCE_HEADER] == SOURCE
    assert json.loads(patched.content) == {
        "project_id": PROJECT_ID,
        "labels": {"omni_project": ""},
    }


async def test_update_maps_a_family_refusal_from_the_server():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/info":
            return httpx.Response(200, json=CHECKED_INFO)
        if request.method == "GET":
            return httpx.Response(200, json={"id": SOURCE, "project_id": PROJECT_ID})
        error = {"code": "conflict", "message": "Only the family representative can move"}
        return httpx.Response(409, json={"error": error})

    async with _client(handler) as client:
        result = json.loads(
            await execute_session_update({"session_id": SOURCE, "project": ""}, client, SOURCE)
        )
    assert "update the moves_with session instead" in result["error"]


async def test_list_pages_projects_and_sessions_until_the_last_page():
    seen: dict[str, list[httpx.QueryParams]] = {"projects": [], "sessions": []}
    page_two = {"id": "9" * 32, "name": "Later"}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == f"/v1/projects/{PROJECT_ID}":
            return httpx.Response(200, json={"id": PROJECT_ID, "name": "Q3"})
        if path == "/v1/projects":
            seen["projects"].append(request.url.params)
            if request.url.params.get("after") == "1" * 32:
                return httpx.Response(200, json={"data": [page_two], "has_more": False})
            first = {"id": "1" * 32, "name": "First"}
            return httpx.Response(
                200, json={"data": [first], "has_more": True, "last_id": first["id"]}
            )
        seen["sessions"].append(request.url.params)
        if request.url.params.get("after") == FAMILY_MEMBER:
            return httpx.Response(200, json={"data": [], "has_more": False})
        rows = [
            {"id": SOURCE, "title": "Me", "status": "running", "project_id": PROJECT_ID},
            {
                "id": FAMILY_MEMBER,
                "status": "idle",
                "labels": {"omni_project": "Legacy folder"},
                "family": {"id": SOURCE, "representative_id": SOURCE},
            },
        ]
        return httpx.Response(200, json={"data": rows, "has_more": True, "last_id": FAMILY_MEMBER})

    async with _client(handler) as client:
        first = json.loads(
            await execute_project_list({"project": "", "limit": 500}, client, SOURCE)
        )
        last = json.loads(
            await execute_project_list(
                {
                    "after": first["next_cursor"],
                    "projects_after": first["projects_next_cursor"],
                },
                client,
                SOURCE,
            )
        )
    assert seen["sessions"][0]["project"] == "" and seen["sessions"][0]["limit"] == "200"
    assert seen["sessions"][0]["visibility"] == "mine" and seen["sessions"][0]["kind"] == "default"
    assert seen["sessions"][0]["sort_by"] == "updated_at"
    assert seen["projects"][0]["limit"] == "200" and "after" not in seen["projects"][0]
    assert first["projects"] == [{"name": "First", "id": "1" * 32}]
    assert first["sessions"][0] == {
        "session_id": SOURCE,
        "title": "Me",
        "status": "running",
        "project": "Q3",
        "this_session": True,
    }
    assert first["sessions"][1]["project"] == "Legacy folder"
    assert first["sessions"][1]["moves_with"] == SOURCE
    assert first["next_cursor"] == FAMILY_MEMBER and first["projects_next_cursor"] == "1" * 32
    assert seen["sessions"][1]["after"] == FAMILY_MEMBER
    assert seen["projects"][1]["after"] == "1" * 32
    assert last["projects"] == [page_two] and last["sessions"] == []
    assert last["next_cursor"] is None and last["projects_next_cursor"] is None


@pytest.mark.parametrize("parent", [None, PARENT])
@pytest.mark.parametrize("effort", [None, "high"])
async def test_update_settings_uses_existing_session_patch_for_children(parent, effort):
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path == "/v1/info":
            return httpx.Response(200, json=CHECKED_INFO)
        if request.method == "GET":
            return httpx.Response(200, json={"permission_level": 4, "parent_session_id": parent})
        return httpx.Response(
            200, json={"model_override": "host-model", "reasoning_effort": effort}
        )

    async with _client(handler) as client:
        result = json.loads(
            await execute_session_update(
                {"session_id": SOURCE, "model": "host-model", "reasoning_effort": effort},
                client,
                SOURCE,
            )
        )
    assert result["status"] == "updated"
    patches = [r for r in requests if r.method == "PATCH"]
    assert len(patches) == 1
    assert json.loads(patches[0].content) == {
        "model_override": "host-model",
        "reasoning_effort": effort,
    }
    assert patches[0].headers[ORCHESTRATION_SOURCE_HEADER] == SOURCE


async def test_update_settings_waits_for_slow_probe_snapshot_and_patch():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        timeout = request.extensions["timeout"]
        io_timeout = 60.0 if request.method == "PATCH" else 30.0
        assert all(timeout[phase] == io_timeout for phase in ("connect", "pool", "write"))
        if request.url.path == "/v1/info":
            delay, reply = 80, CHECKED_INFO
        elif request.method == "GET":
            assert request.url.params["include_usage"] == "false"
            assert request.url.params["include_live_status"] == "false"
            delay, reply = 80, {"permission_level": 4, "parent_session_id": PARENT}
        else:
            assert request.url.params["include_usage"] == "false"
            assert request.headers[ORCHESTRATION_SOURCE_HEADER] == SOURCE
            delay, reply = 630, {"model_override": "host-model", "reasoning_effort": "high"}
        # Simulate reply latencies against the bound HTTPX passes to the transport.
        if timeout["read"] < delay:
            raise httpx.ReadTimeout("")
        return httpx.Response(200, json=reply)

    async with _client(handler) as client:
        result = json.loads(
            await execute_session_update(
                {"session_id": SUB_AGENT, "model": "host-model", "reasoning_effort": "high"},
                client,
                SOURCE,
            )
        )
    assert result == {
        "session_id": SUB_AGENT,
        "status": "updated",
        "model": "host-model",
        "reasoning_effort": "high",
    }
    assert requests == [
        ("GET", "/v1/info"),
        ("GET", f"/v1/sessions/{SUB_AGENT}"),
        ("PATCH", f"/v1/sessions/{SUB_AGENT}"),
    ]


async def test_update_settings_reports_a_snapshot_failure_before_any_patch():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        if request.url.path == "/v1/info":
            return httpx.Response(200, json=CHECKED_INFO)
        raise httpx.ReadTimeout("")

    async with _client(handler) as client:
        result = json.loads(
            await execute_session_update(
                {"session_id": SUB_AGENT, "model": "host-model"}, client, SOURCE
            )
        )
    assert result == {"session_id": SUB_AGENT, "error": "session could not be read: ReadTimeout"}


async def test_update_settings_stops_waiting_at_the_patch_deadline(monkeypatch):
    monkeypatch.setattr(session_projects, "_SETTINGS_PATCH_TIMEOUT_S", 0.01, raising=False)
    patches = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/info":
            return httpx.Response(200, json=CHECKED_INFO)
        if request.method == "GET":
            return httpx.Response(200, json={"permission_level": 4})
        patches.append(request)
        await asyncio.sleep(0.2)
        return httpx.Response(200, json={"model_override": "host-model"})

    async with _client(handler) as client:
        result = json.loads(
            await execute_session_update(
                {"session_id": SUB_AGENT, "model": "host-model"}, client, SOURCE
            )
        )
    assert "PATCH" in result["error"] and "0.01s" in result["error"]
    assert len(patches) == 1


async def test_update_settings_denies_foreign_readable_session():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200, json=CHECKED_INFO if request.url.path == "/v1/info" else {"permission_level": 2}
        )

    async with _client(handler) as client:
        result = json.loads(
            await execute_session_update(
                {"session_id": SOURCE, "model": "host-model"}, client, SOURCE
            )
        )
    assert "owner" in result["error"]
    assert all(r.method == "GET" for r in requests)


@pytest.mark.parametrize(
    "capability",
    [
        "session_move_source_checks",
        "session_settings_source_checks",
        "session_organize_source_checks",
    ],
)
async def test_settings_require_a_server_that_checks_setting_sources(capability):
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path == "/v1/info":
            return httpx.Response(200, json={capability: True})
        if request.method == "GET":
            return httpx.Response(200, json={"permission_level": 4})
        return httpx.Response(200, json={"model_override": "host-model"})

    async with _client(handler) as client:
        result = json.loads(
            await execute_session_update(
                {"session_id": SOURCE, "model": "host-model"}, client, SOURCE
            )
        )
    if capability == "session_move_source_checks":
        assert "cannot check" in result["error"]
        assert len(requests) == 1
    else:
        assert result["model"] == "host-model"
