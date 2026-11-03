"""Parent answers at the real tool/HTTP/resolver boundaries, with isolated stores."""

import asyncio
import json
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from omnigent.pi_extension_ui import (
    parse_approval_declaration,
    to_elicitation_params,
    to_ui_response,
)
from omnigent.runner.identity import RUNNER_TUNNEL_TOKEN_HEADER, token_bound_runner_id
from omnigent.runner.tool_dispatch import execute_tool
from omnigent.runtime import pending_elicitations
from omnigent.server import child_approvals as approvals
from omnigent.server.routes._sessions import helpers, orchestration
from omnigent.server.routes.sessions import routes_hooks as hooks
from omnigent.server.schemas import ElicitationRequestParams, ElicitationResult
from omnigent.spec import AgentSpec
from omnigent.stores.conversation_store import SUCCEEDED_BY_LABEL_KEY, SUCCEEDS_LABEL_KEY
from tests._helpers.polling import poll_until_async
from tests.server.helpers import create_test_agent


def _pi_request(command="rm -rf tmp/probe", **changes):
    declaration = {
        "scope": "once",
        "action": "shell",
        "command": command,
        "cwd": "/opt/work/omnigent/fork/wt",
        "approve": "Run once",
        "deny": "Block",
    }
    declaration.update(changes)
    return {
        "id": "dialog",
        "method": "select",
        "timeout": 120_000,
        "title": "omnigent-approval/1 " + json.dumps(declaration) + "\nApprove this call",
        "options": ["Block", "Run once"],
    }


def _claude_params(command="echo ready", tool="Bash", **extras):
    return ElicitationRequestParams(
        mode="form",
        message="Permission required",
        requestedSchema=None,
        url=None,
        phase="pre_tool_use",
        policy_name="claude_native_permission",
        tool_name=tool,
        command=command,
        **extras,
    )


@pytest.fixture(autouse=True)
async def _offer_tasks():
    yield
    tasks = [
        *approvals._deadlines.values(),
        *approvals._deliveries.values(),
        *helpers._terminal_resolution_tasks.values(),
    ]
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    approvals._deadlines.clear()
    approvals._deliveries.clear()
    helpers._terminal_resolution_tasks.clear()


@pytest.fixture
async def tree(app, client, monkeypatch):
    agent = await create_test_agent(client)
    root_id = agent["_session_id"]
    response = await client.post(
        "/v1/sessions", json={"agent_id": agent["id"], "parent_session_id": root_id}
    )
    assert response.status_code == 201, response.text
    child_id = response.json()["id"]
    service = app.state.standing_approval_service
    root = service.store.get_conversation(root_id)
    service.change(root, "grant", [approvals.APPROVAL_KIND], "approval:fixture")
    binding = "test-parent-binding"
    service.store.set_runner_id(root_id, token_bound_runner_id(binding))
    client.headers[RUNNER_TUNNEL_TOKEN_HEADER] = binding
    offer = AsyncMock(return_value=True)
    monkeypatch.setattr(orchestration, "_post_system_message", offer)
    return SimpleNamespace(
        root_id=root_id,
        child_id=child_id,
        store=service.store,
        service=service,
        client=client,
        offer=offer,
    )


async def _publish(tree, params, elicitation_id="elicit_pi_dialog"):
    event = {
        "type": "response.elicitation_request",
        "elicitation_id": elicitation_id,
        "params": params.model_dump(),
    }
    await approvals.publish_request(tree.child_id, event, tree.store)
    await poll_until_async(lambda: tree.offer.await_count > 0, timeout_s=5)
    return event


async def _answer(tree, decision="approve", caller_id=None, **extra):
    return json.loads(
        await execute_tool(
            tool_name="sys_approval_answer",
            arguments=json.dumps(
                {
                    "session_id": tree.child_id,
                    "elicitation_id": "elicit_pi_dialog",
                    "decision": decision,
                    "reason": "Read-only or inside the assigned workspace",
                    **extra,
                }
            ),
            conversation_id=caller_id or tree.root_id,
            server_client=tree.client,
            agent_spec=AgentSpec(spec_version=1),
        )
    )


def _pending(tree):
    entry = pending_elicitations.lookup("elicit_pi_dialog", conversation_id=tree.child_id)
    return entry[1]["params"] if entry else None


@pytest.mark.parametrize(
    "command,user_only",
    [
        ("rm -rf tmp/probe", False),
        ("/bin/rm -rf /etc", True),
        ("rm --recursive --force /etc/app", True),
        ("sudo -n /bin/rm -rf /", True),
        ("CI=/opt/work /usr/bin/sudo -n /bin/rm -rf /etc", True),
        ("bash -lc 'git push --force origin main'", True),
        ("/bin/bash -lc 'rm -rf tmp/probe'", True),
        ("git commit -m 'a; b'", False),
        ("git commit -m ';'", False),
        ("echo $(rm -rf /etc)", True),
        ("echo `whoami`", True),
        ("diff <(echo a) b", True),
        ("eval 'echo a'", True),
        ("source script.sh", True),
        ("cat script.sh | sh", True),
        ("cat input | bash -lc 'echo ready'", True),
        ("python3 -c 'print(1)'", True),
        ("/usr/bin/node --eval='console.log(1)'", True),
        ("echo 'unterminated", True),
        ("bash -lc 'zsh -lc \"sh -c echo\"'", True),
        ('bash -lc \'zsh -lc "sh -c \\"bash -c echo\\""\'', True),
        ("env bash -lc 'rm -rf /etc'", True),
        ("{ /bin/rm -rf /etc; }", True),
        ("if true; then /bin/rm -rf /etc; fi", True),
        ("while true; do git push --force origin main; done", True),
        ('for dir in /etc; do rm -rf "$dir"; done', True),
        ("case x in x) rm -rf /etc;; esac", True),
        ("time /bin/rm -rf /etc", True),
        ("! /bin/rm -rf /etc", True),
        ("rm -rf /e{tc,foo}", True),
        ("${CMD} -rf /etc", True),
        ("bash -lc 'if true; then /bin/rm -rf /etc; fi'", True),
        (">tmp/log /bin/rm -rf /etc", True),
        ("r\\\nm -rf /etc", True),
        ("git commit -m 'a > b'", False),
        ("ls -la", False),
        ("git status", False),
        ("python3 script.py", False),
        ("nohup rm -rf /etc", True),
        ("timeout 5 rm -rf /etc", True),
        ("nice rm -rf /etc", True),
        ("ionice rm -rf /etc", True),
        ("stdbuf -oL rm -rf /etc", True),
        ("setsid rm -rf /etc", True),
        ("xargs rm -rf /etc", True),
        ("parallel rm -rf /etc", True),
        ("watch rm -rf /etc", True),
        ("chroot / rm -rf /etc", True),
        ("flock /tmp/lock rm -rf /etc", True),
        ("unshare rm -rf /etc", True),
        ("busybox rm -rf /etc", True),
        ("doas rm -rf /etc", True),
        ("ssh host rm -rf /etc", True),
        ("sudo -s 'rm -rf /etc'", True),
        ("/usr/bin/sudo -s 'rm -rf /etc'", True),
        ("su -c 'rm -rf /etc'", True),
        ("runuser -u root -- rm -rf /etc", True),
        ("sg root -c 'rm -rf /etc'", True),
        ("chrt 1 rm -rf /etc", True),
        ("taskset 1 rm -rf /etc", True),
        ("numactl --localalloc rm -rf /etc", True),
        ("nsenter -t 1 -- rm -rf /etc", True),
        ("systemd-run rm -rf /etc", True),
        ("screen rm -rf /etc", True),
        ("tmux new-session 'rm -rf /etc'", True),
        ("script -c 'rm -rf /etc'", True),
        ("nohup ls -la", True),
        ("/usr/bin/nohup rm -rf /etc", True),
        ("bash -c 'nohup rm -rf /etc'", True),
        ("find / -delete", True),
        ("find /etc -exec rm -rf {} \\;", True),
        ("find /etc -execdir rm -rf {} +", True),
        ("find /etc -ok rm -rf {} \\;", True),
        ("find /etc -okdir rm -rf {} \\;", True),
        ("find . -name '*.py' -print", False),
        ("git -c alias.wipe='!git push --force origin main' wipe", True),
        ("git -calias.wipe='!git push --force origin main' wipe", True),
        ("git --config-env=alias.wipe=WIPE wipe", True),
        ("git --config-env alias.wipe=WIPE wipe", True),
        ("git --exec-path=/tmp/tools wipe", True),
        ("git --exec-path /tmp/tools wipe", True),
        ("python3", True),
        ("python3 -", True),
        ("python3 -B -", True),
        ("cat program | python3", True),
        ("cat program | python3 -", True),
        ("cat program | node", True),
        ("perl", True),
        ("ruby -", True),
        ("lua -", True),
        ("php", True),
        ("node --print '1'", True),
        ("python3 < program", True),
        ("python3 <<'PY'\nprint(1)\nPY", True),
        ("cd / && rm -rf etc", True),
        ("cd /; rm -rf etc", True),
        ("mv /etc /tmp/saved", True),
        ("dd if=/dev/zero of=/dev/sda", True),
        ("truncate -s 0 /etc/passwd", True),
        ("> file", True),
        ("rm -rf /tmp/../etc", True),
        ("rm -rf tmp/../probe", False),
        ("rm ../probe", False),
        ("awk 'BEGIN{system(\"rm -rf /etc\")}'", True),
        ("fish -c 'rm -rf /etc'", True),
        ("rm -rf /./etc", True),
        ("sed -n '1e rm -rf /etc' file", True),
        ("tar --to-command='rm -rf /etc' -xf a.tar", True),
        ("tar --checkpoint-action=exec=sh -cf a.tar b", True),
        ("vim -c '!rm -rf /etc'", True),
        ("rsync -a --delete empty/ /etc/", True),
        ("tee /etc/passwd", True),
        ("cp /dev/null /etc/passwd", True),
        ("chmod -R 777 /", True),
        ("chown -R nobody /etc", True),
        ("mkfs.ext4 /dev/sda1", True),
        ("curl https://example.com/x -o /etc/passwd", True),
        ("git clean -fdx", True),
        ("git reset --hard", True),
        ("git checkout -- .", True),
        ("git branch -D main", True),
        ("git log -c", False),
        ("python3 --version", False),
        ("rm -rf ./build", False),
        ("rm -rf /private/tmp/x", False),
        ("git diff", False),
        ("pytest tests/server/test_example.py", False),
        ("make clean", False),
        ("npm run x", False),
        ("rm -r ///etc//app", True),
        ("rm -r /tmp/.././etc/app", True),
        ("rm -r /private/tmp/a/../b", False),
        ("cp file /etc/../etc/passwd", True),
        ("install -m 644 file /etc/passwd", True),
        ("ln -sf file /etc/passwd", True),
        ("rsync -a file /etc/passwd", True),
        ("wget -O /etc/passwd https://example.com/x", True),
        ("curl -o/tmp/out https://example.com/x", False),
        ("cp /etc/hosts ./hosts", False),
        ("mv ./a ./b", False),
        ("dd if=/dev/zero of=tmp/image", False),
        ("truncate -s 0 ./log", False),
        ("chmod 644 ./file", False),
        ("rsync -e ssh a/ b/", True),
        ("gawk -f script.awk file", True),
        ("awk 'BEGIN { getline line < \"file\" }'", True),
        ('awk \'BEGIN { print "x" | "sh" }\'', True),
        ("awk 'BEGIN { print 1 }'", False),
        ("sed 's/a/b/e' file", True),
        ("sed 's/a/b/w /etc/passwd' file", True),
        ("sed -n 'w /etc/passwd' file", True),
        ("sed 's/new/old/' file", False),
        ("git -C repo log -c", False),
        ("git -C repo -c alias.x=anything x", True),
        ("git restore file", True),
        ("git tag -d v1", True),
        ("git stash drop", True),
        ("git stash clear", True),
        ("git filter-branch --all", True),
        ("git update-ref -d refs/heads/main", True),
        ("git reflog expire --expire=now --all", True),
        ("sed '1s/x/y/w /etc/passwd' file", True),
        ("sed '1,2s/x/y/e' file", True),
        ("sed '/x/s/x/y/w /etc/passwd' file", True),
        ("sed '1s/x/y/' file", False),
        ("chmod -R -w /etc", True),
        ("chmod -w ./file", False),
        ("git checkout -f main", True),
        ("git checkout --force main", True),
        ("git checkout main", False),
        ("tar -cI /tmp/filter -f a.tar file", True),
        ("tar -cf a.tar file", False),
        ("git diff HEAD~1", False),
        ("git show HEAD~2:README.md", False),
        ("ls ~", False),
        ("cat ~/notes.txt", False),
        ("rm -rf ~/work", True),
        ("bash infra/fork-ci-check.sh . tests/x.py", False),
        ("sh script.sh", False),
        ("cmd /c 'echo ready'", True),
        *[
            (f"{shell} -c 'echo ready'", True)
            for shell in ("ksh", "csh", "tcsh", "dash", "pwsh", "powershell", "cmd")
        ],
        *[(f"{editor} file", True) for editor in ("vi", "nvim", "emacs", "ed", "ex")],
        *[
            (f"{tool} argument", True)
            for tool in ("fdisk", "diskutil", "shutdown", "reboot", "launchctl", "systemctl")
        ],
    ],
)
def test_floor_preserves_quoting_and_fails_closed(command, user_only):
    params = _claude_params(command).model_dump()
    assert approvals.classify_card(params) == ("note_only" if user_only else "answer")
    pi = to_elicitation_params(_pi_request(command)).model_dump()
    assert approvals.classify_card(pi) == ("note_only" if user_only else "answer")


@pytest.mark.parametrize(
    "changes",
    [
        {"phase": "unknown"},
        {"policy_name": "ask_on_tools"},
        {"phase": "codex_permissions_approval"},
        {"native_delivery": {}},
        {"requestedSchema": {"properties": {"x": {"type": "string"}}}},
        {"tool_name": "AskUserQuestion"},
        {"tool_name": "ExitPlanMode"},
        {"tool_name": "EnterPlanMode"},
        {"tool_name": "request_user_input"},
        {"tool_name": "sys_standing_approval"},
        {"tool_name": "mcp__omnigent__sys_standing_pass"},
        {"tool_name": ""},
        {"tool_name": None},
        {"ask_user_question": {"questions": [{}]}},
    ],
)
def test_unoffered_sources_and_user_owned_tools(changes):
    params = {**_claude_params().model_dump(), **changes}
    assert approvals.classify_card(params) is None


@pytest.mark.parametrize(
    "changes",
    [
        {"scope": "session"},
        {"action": "file"},
        {"command": ""},
        {"approve": "Other"},
        {"deny": "Run once"},
        {"cwd": {}},
    ],
)
def test_invalid_pi_declaration_remains_a_user_question(changes):
    request = _pi_request(**changes)
    approval, title = parse_approval_declaration(request)
    assert approval is None and title == request["title"]
    params = to_elicitation_params(request).model_dump()
    assert params["message"] == request["title"]
    assert approvals.classify_card(params) is None


def test_pi_declaration_preserves_human_options_and_bounds_command():
    request = _pi_request()
    params = to_elicitation_params(request).model_dump()
    assert params["message"] == "Approve this call"
    assert params["ask_user_question"]["questions"][0]["options"] == [
        {"label": "Block"},
        {"label": "Run once"},
    ]
    assert approvals.classify_card(params) == "answer"
    for malformed in [
        {**request, "options": [[], "Run once"]},
        {**request, "options": ["Run once", "Block", "Always"]},
        {**request, "method": "confirm"},
        {**request, "title": "omnigent-approval/1 {invalid}\nHuman question"},
        _pi_request("x" * (16 * 1024 + 1)),
    ]:
        assert parse_approval_declaration(malformed)[0] is None


@pytest.mark.parametrize("decision,label", [("approve", "Run once"), ("decline", "Block")])
async def test_runner_owned_select_answer_through_tool(tree, monkeypatch, decision, label):
    request = _pi_request()
    forwarded = []

    async def receive(req):
        forwarded.append(json.loads(req.content))
        return httpx.Response(200, json={"accepted": True})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(receive), base_url="http://runner"
    ) as runner:
        monkeypatch.setattr(helpers, "_get_runner_client", AsyncMock(return_value=runner))
        await _publish(tree, to_elicitation_params(request))
        assert pending_elicitations.count_for(tree.child_id) == 1
        assert pending_elicitations.display_count(tree.child_id, 1) == 0
        assert pending_elicitations.display_count(tree.root_id, 1) == 0
        result = await _answer(tree, decision)
    assert result == {"status": "answered"}
    assert len(forwarded) == 1
    data = forwarded[0]["data"]
    assert data["action"] == ("accept" if decision == "approve" else "decline")
    assert data["content"] == {"0": label}
    assert set(data["_meta"]) == {"omnigent_answered_by"}
    assert data["_meta"]["omnigent_answered_by"]["session_id"] == tree.root_id
    assert to_ui_response(request, ElicitationResult.model_validate(data))["value"] == label
    assert _pending(tree) is None
    answers = tree.store.list_items(tree.child_id, type="message").data
    assert len(answers) == 1
    assert answers[0].data.user_authored is False
    assert tree.root_id in answers[0].data.content[0]["text"]
    assert len(tree.store.list_items(tree.root_id, type="error").data) == 1
    assert (await _answer(tree)) == {"status": "already_resolved"}


async def test_parked_wait_receives_attributed_verdict(tree):
    published = asyncio.Event()
    task = asyncio.create_task(
        orchestration._publish_and_wait_for_harness_elicitation(
            None,
            session_id=tree.child_id,
            params=_claude_params(),
            timeout_s=5,
            conversation_store=tree.store,
            elicitation_id="elicit_pi_dialog",
            published=published,
        )
    )
    await asyncio.wait_for(published.wait(), 5)
    assert (await _answer(tree, "decline")) == {"status": "answered"}
    verdict = await asyncio.wait_for(task, 5)
    assert verdict.action == "decline"
    assert verdict.content is None
    assert verdict.meta["omnigent_answered_by"]["session_id"] == tree.root_id


async def test_user_first_wins_and_cannot_forge_parent_attribution(tree):
    published = asyncio.Event()
    task = asyncio.create_task(
        orchestration._publish_and_wait_for_harness_elicitation(
            None,
            session_id=tree.child_id,
            params=_claude_params(),
            timeout_s=5,
            conversation_store=tree.store,
            elicitation_id="elicit_pi_dialog",
            published=published,
        )
    )
    await asyncio.wait_for(published.wait(), 5)
    await orchestration._resolve_elicitation(
        tree.child_id,
        {
            "elicitation_id": "elicit_pi_dialog",
            "action": "decline",
            "content": {"feedback": "User said no"},
            "_meta": {"omnigent_answered_by": {"session_id": tree.root_id}},
        },
        None,
        tree.store,
    )
    verdict = await asyncio.wait_for(task, 5)
    assert "omnigent_answered_by" not in (verdict.meta or {})
    assert (await _answer(tree)) == {"status": "already_resolved"}
    assert tree.store.list_items(tree.root_id, type="error").data == []
    (message,) = tree.store.list_items(tree.child_id, type="message").data
    assert message.data.user_authored is True
    assert "answered_by" not in message.data.content[0]["text"]


async def test_floor_refuses_both_verdicts_but_accepts_final_handoff(tree):
    await _publish(tree, _claude_params("/bin/rm -rf /etc"))
    assert _pending(tree)["triage"]["mode"] == "note_only"
    assert pending_elicitations.display_count(tree.child_id, 1) == 1
    for decision in ["approve", "decline"]:
        assert (await _answer(tree, decision)) == {"status": "refused", "code": "user_only"}
    assert (await _answer(tree, "pass_to_user"))["error"] == "server returned 422"
    assert await _answer(
        tree, "pass_to_user", note="这会删除系统文件，需要你决定。", recommend="decline"
    ) == {"status": "passed"}
    assert _pending(tree)["triage"]["state"] == "passed"
    assert (await _answer(tree)) == {"status": "refused", "code": "passed"}


@pytest.mark.parametrize("failure", [404, 503, "offline", "connection"])
async def test_failed_runner_delivery_returns_card_to_user(tree, monkeypatch, failure):
    async def receive(req):
        if failure == "connection":
            raise httpx.ConnectError("runner unavailable", request=req)
        return httpx.Response(failure, json={"error": "expired"})

    await _publish(tree, to_elicitation_params(_pi_request()))
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(receive), base_url="http://runner"
    ) as runner:
        monkeypatch.setattr(
            helpers,
            "_get_runner_client",
            AsyncMock(return_value=None if failure == "offline" else runner),
        )
        assert (await _answer(tree)) == {"status": "delivery_failed"}
    assert _pending(tree)["triage"]["state"] == "unanswered"
    assert pending_elicitations.display_count(tree.child_id, 1) == 1
    codes = {item.data.code for item in tree.store.list_items(tree.root_id, type="error").data}
    assert codes == {"child_approval_answered", "child_approval_undelivered"}


async def test_receipt_failure_keeps_wait_and_does_not_forward(tree, monkeypatch):
    await _publish(tree, to_elicitation_params(_pi_request()))
    append = tree.store.append

    def fail_receipt(session_id, items):
        if session_id == tree.root_id:
            raise RuntimeError("disk unavailable")
        return append(session_id, items)

    monkeypatch.setattr(tree.store, "append", fail_receipt)
    runner_lookup = AsyncMock()
    monkeypatch.setattr(helpers, "_get_runner_client", runner_lookup)
    assert await _answer(tree) == {"status": "refused", "code": "receipt_failed"}
    assert _pending(tree)["triage"]["state"] == "offered"
    runner_lookup.assert_not_awaited()


async def test_stalled_receipt_releases_lock_for_deadline_and_user(tree, monkeypatch):
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    append = tree.store.append

    def stalled_append(session_id, items):
        if session_id == tree.root_id:
            entered.set()
            try:
                assert release.wait(5), "test must release the blocked database write"
                return append(session_id, items)
            finally:
                finished.set()
        return append(session_id, items)

    monkeypatch.setattr(tree.store, "append", stalled_append)
    monkeypatch.setattr(approvals, "RECEIPT_TIMEOUT_S", 0.05)
    # The database stays stalled across the receipt limit and the display deadline.
    monkeypatch.setattr(approvals, "OFFER_TIMEOUT_S", 0.2)
    await _publish(tree, _claude_params())
    future = asyncio.get_running_loop().create_future()
    orchestration._harness_elicitation_registry["elicit_pi_dialog"] = future
    orchestration._harness_elicitation_owners["elicit_pi_dialog"] = tree.child_id
    runner_lookup = AsyncMock(return_value=None)
    monkeypatch.setattr(helpers, "_get_runner_client", runner_lookup)
    answer = asyncio.create_task(_answer(tree))
    try:
        await poll_until_async(entered.is_set, timeout_s=2)
        assert await asyncio.wait_for(asyncio.shield(answer), 0.5) == {
            "status": "refused",
            "code": "receipt_failed",
        }
        await poll_until_async(
            lambda: _pending(tree)["triage"]["state"] == "unanswered", timeout_s=1
        )
        assert not release.is_set()
        assert pending_elicitations.display_count(tree.child_id, 1) == 1
        runner_lookup.assert_not_awaited()
        assert (
            await asyncio.wait_for(
                orchestration._resolve_elicitation(
                    tree.child_id,
                    {"elicitation_id": "elicit_pi_dialog", "action": "decline"},
                    runner_router=None,
                    conversation_store=tree.store,
                ),
                1,
            )
            is None
        )
        assert future.result().action == "decline"
    finally:
        release.set()
        await asyncio.gather(answer, return_exceptions=True)
        await poll_until_async(finished.is_set, timeout_s=2)


@pytest.mark.parametrize("raises", [False, True])
async def test_offer_failure_drops_stamp(tree, raises):
    tree.offer.side_effect = RuntimeError("parent offline") if raises else None
    tree.offer.return_value = False
    await _publish(tree, _claude_params())
    await poll_until_async(lambda: "triage" not in (_pending(tree) or {}), timeout_s=5)
    assert await _answer(tree) == {"status": "refused", "code": "not_offered"}


async def test_deadline_is_independent_of_stalled_offer_delivery(tree, monkeypatch):
    gate = asyncio.Event()

    async def stall(*args, **kwargs):
        await gate.wait()
        return True

    tree.offer.side_effect = stall
    monkeypatch.setattr(approvals, "OFFER_TIMEOUT_S", 0.02)
    await approvals.publish_request(
        tree.child_id,
        {
            "type": "response.elicitation_request",
            "elicitation_id": "elicit_pi_dialog",
            "params": _claude_params().model_dump(),
        },
        tree.store,
    )
    await poll_until_async(lambda: _pending(tree)["triage"]["state"] == "unanswered", timeout_s=5)
    assert not gate.is_set()
    assert await _answer(
        tree, "pass_to_user", note="父会话未及时答复，请检查这次调用。", recommend="approve"
    ) == {"status": "passed"}
    gate.set()


async def test_offer_identity_and_live_revoke(tree):
    await _publish(tree, _claude_params())
    # A sibling/child cannot use the parent's binding to name itself as caller.
    other = await _answer(tree, caller_id=tree.child_id)
    assert other["error"] == "server returned 403"
    tree.store.set_runner_id(tree.child_id, token_bound_runner_id("test-parent-binding"))
    assert await _answer(tree, caller_id=tree.child_id) == {
        "status": "refused",
        "code": "not_answerer",
    }
    outsider = tree.store.create_conversation(
        runner_id=token_bound_runner_id("test-parent-binding")
    )
    assert await _answer(tree, caller_id=outsider.id) == {
        "status": "refused",
        "code": "not_answerer",
    }
    root = tree.store.get_conversation(tree.root_id)
    tree.service.change(root, "revoke", [approvals.APPROVAL_KIND], "user-revoked")
    assert await _answer(tree) == {"status": "refused", "code": "approval_revoked"}
    assert _pending(tree)["triage"]["state"] == "offered"
    forged = await tree.client.post(
        f"/v1/sessions/{tree.root_id}/child-approvals/{tree.child_id}/elicit_pi_dialog",
        json={"decision": "approve", "reason": "test", "caller_id": tree.root_id},
    )
    assert forged.status_code == 422


async def test_off_and_later_enabled_root_never_acquire_an_old_card(tree):
    root = tree.store.get_conversation(tree.root_id)
    tree.service.change(root, "revoke", [approvals.APPROVAL_KIND], "user-revoked")
    event = {
        "type": "response.elicitation_request",
        "elicitation_id": "elicit_pi_dialog",
        "params": _claude_params().model_dump(),
    }
    await approvals.publish_request(tree.child_id, event, tree.store)
    assert "triage" not in _pending(tree)
    tree.offer.assert_not_awaited()
    tree.service.change(root, "grant", [approvals.APPROVAL_KIND], "approval:new")
    assert await _answer(tree) == {"status": "refused", "code": "not_offered"}
    await approvals.publish_request(tree.child_id, event, tree.store)
    assert "triage" not in _pending(tree)
    tree.offer.assert_not_awaited()


async def test_repark_adopts_same_offer_but_changed_call_returns_to_user(tree):
    original = await _publish(tree, _claude_params())
    first_triage = dict(_pending(tree)["triage"])
    await approvals.publish_request(
        tree.child_id,
        {
            **original,
            "params": _claude_params().model_dump(),
        },
        tree.store,
    )
    assert _pending(tree)["triage"] == first_triage
    tree.offer.assert_awaited_once()
    await approvals.publish_request(
        tree.child_id,
        {
            **original,
            "params": _claude_params("echo different").model_dump(),
        },
        tree.store,
    )
    assert "triage" not in _pending(tree)
    assert await _answer(tree) == {"status": "refused", "code": "not_offered"}


async def test_successor_holds_one_hop_and_predecessor_loses_it(tree, monkeypatch):
    await _publish(tree, _claude_params())
    successor = tree.store.create_conversation(
        runner_id=token_bound_runner_id("test-parent-binding")
    )
    tree.store.set_labels(successor.id, {SUCCEEDS_LABEL_KEY: tree.root_id})
    tree.store.set_labels(tree.root_id, {SUCCEEDED_BY_LABEL_KEY: successor.id})
    assert await _answer(tree) == {"status": "refused", "code": "inactive_parent"}
    assert await _answer(tree, caller_id=successor.id) == {
        "status": "refused",
        "code": "not_answerer",
    }
    monkeypatch.setattr(
        tree.store, "get_succession", lambda old, new: SimpleNamespace(phase="moved")
    )
    assert await _answer(tree, caller_id=successor.id) == {
        "status": "refused",
        "code": "not_answerer",
    }
    monkeypatch.setattr(
        tree.store, "get_succession", lambda old, new: SimpleNamespace(phase="done")
    )
    assert await _answer(
        tree, "pass_to_user", caller_id=successor.id, note="请决定。", recommend="approve"
    ) == {"status": "passed"}


async def test_detached_verdict_attribution_and_single_use_grant(tree, monkeypatch):
    deliveries = []

    async def delivered(session_id, text, **kwargs):
        deliveries.append(text)
        return True

    monkeypatch.setattr(hooks, "_deliver_with_retry", delivered)
    await hooks._open_detached_card(
        tree.child_id,
        _claude_params(),
        elicitation_id="elicit_pi_dialog",
        kind="approval",
        payload={"aid": "a1", "action": "Bash(echo ready)"},
        grant_key=("one-call",),
        mirror=True,
        conversation_store=tree.store,
        runner_router=None,
    )
    await poll_until_async(lambda: _pending(tree) is not None, timeout_s=5)
    assert await _answer(tree) == {"status": "answered"}
    await poll_until_async(lambda: bool(deliveries), timeout_s=5)
    assert "granted by the parent session" in deliveries[-1]
    grant = await hooks._consume_grant(tree.child_id, ("one-call",), tree.store)
    assert grant.action == "accept"
    assert grant.meta["omnigent_answered_by"]["session_id"] == tree.root_id
    assert await hooks._consume_grant(tree.child_id, ("one-call",), tree.store) is None


@pytest.mark.parametrize("decision", ["approve", "pass_to_user"])
@pytest.mark.parametrize("source", ["tool_result", "by_id"])
async def test_native_signal_during_parent_receipt_has_one_serialized_winner(
    tree, monkeypatch, decision, source
):
    published = asyncio.Event()
    waiter = asyncio.create_task(
        orchestration._publish_and_wait_for_harness_elicitation(
            None,
            session_id=tree.child_id,
            params=_claude_params(),
            timeout_s=5,
            conversation_store=tree.store,
            elicitation_id="elicit_pi_dialog",
            published=published,
            tool_name="Bash",
            tool_input={"command": "echo ready"},
        )
    )
    await asyncio.wait_for(published.wait(), 5)
    entered, release = asyncio.Event(), asyncio.Event()
    receipt = approvals._receipt

    async def paused_receipt(*args, **kwargs):
        entered.set()
        await release.wait()
        return await receipt(*args, **kwargs)

    monkeypatch.setattr(approvals, "_receipt", paused_receipt)
    answer = asyncio.create_task(
        _answer(
            tree,
            decision,
            **(
                {"note": "请决定这次调用。", "recommend": "approve"}
                if decision == "pass_to_user"
                else {}
            ),
        )
    )
    await asyncio.wait_for(entered.wait(), 5)
    from omnigent.server._elicitation_registry import _harness_parked_elicitations

    parked = _harness_parked_elicitations["elicit_pi_dialog"]
    if source == "tool_result":
        helpers._signal_terminal_resolved_harness_elicitation_impl(
            tree.child_id, "Bash", {"command": "echo ready"}
        )
    else:
        helpers._signal_harness_elicitation_resolved_by_id(tree.child_id, "elicit_pi_dialog")
    assert not parked.resolved_elsewhere.is_set()
    assert _pending(tree) is not None
    release.set()
    expected = "passed" if decision == "pass_to_user" else "answered"
    assert await asyncio.wait_for(answer, 5) == {"status": expected}
    verdict = await asyncio.wait_for(waiter, 5)
    if decision == "approve":
        assert verdict.action == "accept"
        assert verdict.meta["omnigent_answered_by"]["session_id"] == tree.root_id
    else:
        assert verdict is None
    assert _pending(tree) is None
    assert len(tree.store.list_items(tree.root_id, type="error").data) == 1


async def test_native_first_writes_no_parent_receipt(tree):
    published = asyncio.Event()
    waiter = asyncio.create_task(
        orchestration._publish_and_wait_for_harness_elicitation(
            None,
            session_id=tree.child_id,
            params=_claude_params(),
            timeout_s=5,
            conversation_store=tree.store,
            elicitation_id="elicit_pi_dialog",
            published=published,
            tool_name="Bash",
            tool_input={"command": "echo ready"},
        )
    )
    await asyncio.wait_for(published.wait(), 5)
    helpers._signal_terminal_resolved_harness_elicitation_impl(
        tree.child_id, "Bash", {"command": "echo ready"}
    )
    assert await _answer(tree) == {"status": "already_resolved"}
    assert await asyncio.wait_for(waiter, 5) is None
    assert tree.store.list_items(tree.root_id, type="error").data == []


@pytest.mark.parametrize("source", ["tool_result", "by_id"])
@pytest.mark.parametrize("same_question", [True, False])
@pytest.mark.parametrize("long_inputs", [False, True])
@pytest.mark.parametrize("signal_before_retry", [False, True])
async def test_queued_native_signal_follows_same_question_repark(
    tree, monkeypatch, source, same_question, long_inputs, signal_before_retry
):
    from omnigent.server._elicitation_registry import _harness_parked_elicitations

    async def park(command, published):
        tool_name = "Write" if long_inputs else "Bash"
        tool_input = (
            {"file_path": "/tmp/a", "content": "a" * 1100 + command}
            if long_inputs
            else {"command": command}
        )
        params = (
            _claude_params("", tool=tool_name, content_preview=json.dumps(tool_input)[:1024])
            if long_inputs
            else _claude_params(command)
        )
        return await orchestration._publish_and_wait_for_harness_elicitation(
            None,
            session_id=tree.child_id,
            params=params,
            timeout_s=5,
            conversation_store=tree.store,
            elicitation_id="elicit_pi_dialog",
            published=published,
            tool_name=tool_name,
            tool_input=tool_input,
        )

    published = asyncio.Event()
    clears_before = set(orchestration._deferred_elicitation_clear_tasks)
    first = asyncio.create_task(park("echo ready", published))
    await asyncio.wait_for(published.wait(), 5)
    first.cancel()
    await asyncio.gather(first, return_exceptions=True)
    clears = set(orchestration._deferred_elicitation_clear_tasks) - clears_before
    old = _harness_parked_elicitations["elicit_pi_dialog"]
    entered, release = asyncio.Event(), asyncio.Event()
    receipt = approvals._receipt

    async def paused_receipt(*args, **kwargs):
        entered.set()
        await release.wait()
        return await receipt(*args, **kwargs)

    monkeypatch.setattr(approvals, "_receipt", paused_receipt)
    answer = asyncio.create_task(
        _answer(tree, "pass_to_user", note="请决定这次调用。", recommend="approve")
    )
    await asyncio.wait_for(entered.wait(), 5)

    def signal():
        if source == "tool_result":
            helpers._signal_terminal_resolved_harness_elicitation_impl(
                tree.child_id, old.tool_name, old.tool_input
            )
        else:
            helpers._signal_harness_elicitation_resolved_by_id(tree.child_id, "elicit_pi_dialog")

    lock = approvals.card_lock(tree.child_id, "elicit_pi_dialog")
    if signal_before_retry:
        signal()
        await poll_until_async(lambda: bool(lock._waiters), timeout_s=2)
    retry_published = asyncio.Event()
    retry = asyncio.create_task(
        park("echo ready" if same_question else "echo different", retry_published)
    )
    try:
        await poll_until_async(
            lambda: len(lock._waiters or []) >= (2 if signal_before_retry else 1), timeout_s=2
        )
        if not signal_before_retry:
            signal()
        assert not old.resolved_elsewhere.is_set()
        release.set()
        assert await asyncio.wait_for(answer, 5) == {"status": "passed"}
        await asyncio.wait_for(retry_published.wait(), 5)
        await poll_until_async(lambda: not helpers._terminal_resolution_tasks, timeout_s=2)
        if signal_before_retry:
            await asyncio.wait_for(asyncio.gather(*clears), 2)
        if same_question:
            assert await asyncio.wait_for(asyncio.shield(retry), 0.5) is None
            assert _pending(tree) is None
        else:
            assert not retry.done()
            assert not _harness_parked_elicitations["elicit_pi_dialog"].resolved_elsewhere.is_set()
            if long_inputs:
                assert _pending(tree)["content_preview"] == json.dumps(old.tool_input)[:1024]
            else:
                assert _pending(tree)["command"] == "echo different"
    finally:
        release.set()
        retry.cancel()
        await asyncio.gather(answer, retry, return_exceptions=True)


async def test_pi_hook_repark_preserves_queued_by_id_evidence(tree):
    from omnigent.server._elicitation_registry import _harness_parked_elicitations

    eid = "elicit_pi_" + "e" * 32

    async def park():
        return await tree.client.post(
            f"/v1/sessions/{tree.child_id}/hooks/pi-extension-ui",
            json={"elicitation_id": eid, "request": _pi_request()},
        )

    first = asyncio.create_task(park())
    await poll_until_async(
        lambda: (
            eid in _harness_parked_elicitations
            and pending_elicitations.lookup(eid, conversation_id=tree.child_id) is not None
        ),
        timeout_s=2,
    )
    first.cancel()
    await asyncio.gather(first, return_exceptions=True)
    lock = approvals.card_lock(tree.child_id, eid)
    async with lock:
        retry = asyncio.create_task(park())
        await poll_until_async(lambda: bool(lock._waiters), timeout_s=2)
        helpers._signal_harness_elicitation_resolved_by_id(tree.child_id, eid)
    try:
        response = await asyncio.wait_for(asyncio.shield(retry), 0.5)
        assert response.status_code == 200
        assert pending_elicitations.lookup(eid, conversation_id=tree.child_id) is None
    finally:
        retry.cancel()
        await asyncio.gather(retry, return_exceptions=True)


async def test_deferred_clear_cannot_resurrect_a_card_during_handoff(tree, monkeypatch):
    from omnigent.server.routes import sessions as facade

    monkeypatch.setattr(facade, "_HARNESS_ELICITATION_REPARK_GRACE_S", 0.03)
    published = asyncio.Event()
    waiter = asyncio.create_task(
        orchestration._publish_and_wait_for_harness_elicitation(
            None,
            session_id=tree.child_id,
            params=_claude_params(),
            timeout_s=5,
            conversation_store=tree.store,
            elicitation_id="elicit_pi_dialog",
            published=published,
        )
    )
    await asyncio.wait_for(published.wait(), 5)
    waiter.cancel()
    await asyncio.gather(waiter, return_exceptions=True)
    entered, release = asyncio.Event(), asyncio.Event()
    receipt = approvals._receipt

    async def paused_receipt(*args, **kwargs):
        entered.set()
        await release.wait()
        return await receipt(*args, **kwargs)

    monkeypatch.setattr(approvals, "_receipt", paused_receipt)
    answer = asyncio.create_task(
        _answer(tree, "pass_to_user", note="请决定这次调用。", recommend="approve")
    )
    await asyncio.wait_for(entered.wait(), 5)
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(asyncio.shield(answer), timeout=0.05)
    assert _pending(tree) is not None
    release.set()
    assert await asyncio.wait_for(answer, 5) == {"status": "passed"}
    await poll_until_async(lambda: _pending(tree) is None, timeout_s=5)


def test_tool_advertisement_and_actor_cannot_be_an_argument():
    from omnigent.runner.tool_dispatch import _NATIVE_RELAY_BUILTIN_TOOLS
    from omnigent.tools.manager import ToolManager

    manager = ToolManager(AgentSpec(spec_version=1, spawn=False))
    try:
        schema = next(
            s for s in manager.get_tool_schemas() if s["function"]["name"] == "sys_approval_answer"
        )
        assert "sys_approval_answer" in _NATIVE_RELAY_BUILTIN_TOOLS
        assert schema["function"]["parameters"]["additionalProperties"] is False
        assert "caller_id" not in schema["function"]["parameters"]["properties"]
    finally:
        manager.shutdown()
