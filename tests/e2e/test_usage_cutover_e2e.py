"""Real server, runner and CLI cutover with a credential-free SDK harness."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx

from tests._helpers.live_server import isolated_process_env
from tests._helpers.polling import poll_until
from tests._helpers.server_runner import server_runner
from tests._helpers.session import bundle_files, post_session_bundle

_HOST = "c" * 32
_KEY = b"isolated-usage-test-signing-key-32"


def server_boot() -> None:
    """Inject only test identity, host ownership and a controllable server clock."""
    import omnigent.server.app as server
    import omnigent.usage_export.cutover as cutover
    from omnigent.server.accounts_config import AccountsConfig
    from omnigent.server.auth import UnifiedAuthProvider
    from omnigent.server.device_grant_store import DeviceGrantStore

    class Auth(UnifiedAuthProvider):
        def get_user_id(self, request):
            if not request.headers.get("authorization"):
                return "local"
            user = super().get_user_id(request)
            # The isolated stack's owner is the reserved no-auth principal;
            # production JWT validation still runs for the synthetic account.
            return "local" if user == "usage-test-owner" else user

    original_router = server.create_usage_router

    def router(store, **kwargs):
        provider = Auth(
            source="accounts",
            accounts_config=AccountsConfig(
                cookie_secret=_KEY,
                session_ttl_hours=1,
                base_url="http://127.0.0.1",
                init_admin_password=None,
                invite_ttl_seconds=3600,
                magic_ttl_seconds=300,
            ),
        )
        provider.set_grant_revocation_check(DeviceGrantStore(store.storage_location).is_revoked)
        kwargs.update(
            auth_provider=provider,
            host_registry=SimpleNamespace(
                get=lambda host_id: SimpleNamespace(owner="local") if host_id == _HOST else None,
            ),
        )
        return original_router(store, **kwargs)

    clock = Path(os.environ["USAGE_TEST_CLOCK"])
    cutover._now = lambda: int(clock.read_text()) if clock.exists() else int(time.time())
    server.create_usage_router = router
    from omnigent.cli import main

    main()


def runner_boot() -> None:
    """Replace the external SDK HTTP peer; runner orchestration stays real."""
    from fastapi import FastAPI
    from fastapi.responses import StreamingResponse

    from omnigent.runtime.harnesses.process_manager import HarnessProcessManager
    from omnigent.usage_export.runtime import check_live_route, outbox
    from omnigent.usage_export.transcripts import claude_physical_identity

    harness = FastAPI()

    @harness.post("/v1/sessions/{session_id}/events")
    async def turn(session_id: str):
        async def events():
            for kind, status in [
                ("response.created", "in_progress"),
                ("response.completed", "completed"),
            ]:
                yield (
                    "data: "
                    + json.dumps(
                        {
                            "type": kind,
                            "response": {
                                "id": "fake-response",
                                "status": status,
                                "output": [],
                            },
                        }
                    )
                    + "\n\n"
                )

        if outbox().pending_admission(session_id, "claude-sdk") is None:
            return StreamingResponse(events(), media_type="text/event-stream")
        check_live_route(session_id, "claude-sdk", {})
        source = Path(os.environ["USAGE_TEST_TRANSCRIPTS"]) / session_id / "physical-main.jsonl"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.touch()
        spool = outbox()
        spool.register_source(source, session_id, "claude-sdk", claude_physical_identity(source))
        spool.bind_claude_path(session_id, "physical-main", None, str(source.resolve()))
        stamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        records = [
            {"type": "user", "uuid": "user-1", "timestamp": stamp},
            {
                "type": "assistant",
                "uuid": "assistant-1",
                "timestamp": stamp,
                "sessionId": "physical-main",
                "requestId": "request-1",
                "message": {
                    "id": "message-1",
                    "model": "fake-model",
                    "usage": {"input_tokens": 7, "output_tokens": 3},
                },
            },
            {"type": "system", "subtype": "turn_duration", "uuid": "end-1", "timestamp": stamp},
        ]
        with source.open("a") as handle:
            handle.writelines(json.dumps(record) + "\n" for record in records)
        spool.scan(source)

        return StreamingResponse(events(), media_type="text/event-stream")

    async def client(self, conversation_id, harness_name=None, **kwargs):
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=harness), base_url="http://sdk.test"
        )

    HarnessProcessManager.get_client = client
    from omnigent.runner._entry import main

    main()


def test_hold_refuses_input_drains_reads_receipt_and_releases(tmp_path: Path) -> None:
    from omnigent.server.auth import LEVEL_OWNER
    from omnigent.server.device_grant_store import DeviceGrantStore
    from omnigent.server.routes.device_auth import mint_delegated_token
    from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
    from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore

    root = Path(__file__).resolve().parents[2]
    clock = tmp_path / "clock"
    bootstrap = "from tests.e2e.test_usage_cutover_e2e import server_boot; server_boot()"
    with server_runner(
        tmp_path / "stack",
        server_bootstrap=bootstrap,
        server_env={"USAGE_TEST_CLOCK": str(clock)},
        server_cwd=root,
    ) as stack:
        stack.start_runner(
            bootstrap="from tests.e2e.test_usage_cutover_e2e import runner_boot; runner_boot()",
            cwd=root,
            env={
                "OMNIGENT_USAGE_EXPORT_CAPTURE": "1",
                "USAGE_TEST_TRANSCRIPTS": str(tmp_path / "transcripts"),
            },
        )
        with httpx.Client(base_url=stack.base_url, trust_env=False, timeout=30) as client:
            bundle = bundle_files(
                {
                    "test.yaml": (
                        b"name: usage-test\nprompt: Test\nexecutor:\n"
                        b"  harness: claude-sdk\n  model: fake-model\n"
                    )
                }
            )
            response = post_session_bundle(client.post, "/v1/sessions", bundle)
            response.raise_for_status()
            session_id = response.json()["session_id"]
            store = SqlAlchemyConversationStore(stack.database_uri)
            store.set_host_id(session_id, _HOST, str(stack.workspace))
            assert store.set_runner_id(session_id, stack.runner_id)
            SqlAlchemyPermissionStore(stack.database_uri).grant("local", session_id, LEVEL_OWNER)
            sent = client.post(
                f"/v1/sessions/{session_id}/events",
                json={
                    "type": "message",
                    "data": {"role": "user", "content": [{"type": "input_text", "text": "run"}]},
                },
            )
            assert sent.status_code in {200, 202}, sent.text

            def has_usage():
                reply = client.get("/v1/usage-events")
                assert reply.status_code == 200, reply.text + stack.log_tail()
                return reply.json()["events"]

            try:
                poll_until(has_usage, timeout_s=30, interval_s=1.2)
            except AssertionError as exc:
                raise AssertionError(
                    f"{exc}\ninitial send: {sent.text}\n{stack.log_tail()}"
                ) from exc
            poll_until(lambda: time.time() % 1 < 0.1, timeout_s=3, interval_s=0.02)
            held = client.post(
                "/v1/usage-cutover/holds",
                json={"host_id": _HOST, "harness": "claude-sdk", "generation": 1},
            )
            assert held.status_code == 200, held.text
            refused = client.post(
                f"/v1/sessions/{session_id}/events",
                json={
                    "type": "message",
                    "data": {"role": "user", "content": [{"type": "input_text", "text": "held"}]},
                },
            )
            assert refused.status_code == 409, refused.text
            assert refused.json()["error"]["code"] == "usage_cutover_hold"

            (stack.runner_home / ".omnigent" / "config.yaml").write_text(f"host:\n  id: {_HOST}\n")
            cli_env = isolated_process_env(stack.runner_home, {}, None)

            def cli(*args):
                result = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "omnigent.cli",
                        "usage-export",
                        "cutover",
                        *args,
                        "--server",
                        stack.base_url,
                        "--domain",
                        f"{_HOST},claude-sdk",
                        "--json",
                    ],
                    cwd=root,
                    env=cli_env,
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                assert result.returncode == 0, result.stdout + result.stderr
                return json.loads(result.stdout)

            receipt = cli("drain", "--generation", "1", "--timeout", "10")
            assert receipt["result"] == "drained"
            assert receipt["calls"][0]["server"]["sequence"] >= 1
            assert receipt["calls"][0]["admission_id"] is not None

            now = int(time.time())
            grants = DeviceGrantStore(stack.database_uri)
            grants.create_grant(
                "d" * 32,
                device_code_hash="test-hash",
                user_code="TEST-READER",
                client_id="usage-test",
                scope="usage:read",
                created_at=now,
                expires_at=now + 600,
            )
            grants.approve("d" * 32, user_id="local", now_epoch_seconds=now)
            grants.redeem_approved(
                "d" * 32, refresh_token_hash="test-refresh-hash", now_epoch_seconds=now
            )
            token = mint_delegated_token(
                "usage-test-owner",
                _KEY,
                3600,
                "accounts",
                grant_id="d" * 32,
                client_id="usage-test",
                jti="test-usage",
                scope="usage:read",
            )
            headers = {"Authorization": f"Bearer {token}"}
            page_response = client.get("/v1/usage-events", headers=headers)
            assert page_response.status_code == 200, page_response.text
            page = page_response.json()
            client.get(
                "/v1/usage-events", params={"cursor": page["next_cursor"]}, headers=headers
            ).raise_for_status()
            receipts = client.get(
                "/v1/usage-cutover/receipts",
                params={"host_id": _HOST, "harness": "claude-sdk", "generation": 1},
                headers=headers,
            )
            assert receipts.status_code == 200, receipts.text
            assert receipts.json()["receipts"][-1]["digest"] == receipt["digest"]
            cutover_at = ((now // 3600) + 1) * 3600
            clock.write_text(str(cutover_at))
            stamp = (
                datetime.fromtimestamp(cutover_at, timezone.utc).isoformat().replace("+00:00", "Z")
            )
            consumer = {
                key: receipt[key]
                for key in ("owner_scope_id", "feed_id", "feed_epoch", "domain", "generation")
            }
            consumer.update(
                kind="drained",
                producer_receipt_digest=receipt["digest"],
                acked_through_sequence=receipt["last_committed_sequence"],
                usage_read_grant_id="d" * 32,
                legacy_frozen=True,
                cutover_at=stamp,
                authority_published=True,
                authority_revision=1,
                authority_digest="a" * 64,
            )
            consumer_file = tmp_path / "consumer.json"
            consumer_file.write_text(json.dumps(consumer))
            released = cli(
                "release",
                "--generation",
                "1",
                "--cutover-at",
                stamp,
                "--consumer-receipt",
                str(consumer_file),
            )
            assert released["result"] == "released"
