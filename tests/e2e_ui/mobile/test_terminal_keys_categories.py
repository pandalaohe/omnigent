"""Opt-in phone key proofs across real session, harness and host terminals.

Run with ``OMNIGENT_TERMINAL_PROOF=1`` in an isolated HOME. No native test submits
a prompt: Claude/Codex use the existing mock-provider fixtures; the other CLIs
are driven through their production wrapper specs without pressing Enter.
"""

from __future__ import annotations

import importlib
import json
import os
import re
import shutil
import subprocess
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Any

import httpx
import pytest
from playwright.sync_api import Browser, Locator, Page, expect

from tests._helpers.browser import user_page
from tests._helpers.host import isolated_host
from tests._helpers.polling import poll_until
from tests._helpers.session import bind_session_runner, bundle_files, post_session_bundle
from tests.e2e_ui.conftest import _server_state, configure_mock_llm, reset_mock_llm
from tests.e2e_ui.mobile.test_terminal_keys import (
    _KEYECHO_SOURCE,
    _PAGE_0_KEYS,
    _PAGE_1_KEYS,
    _assert_key_bytes,
    _lines,
    _press_soft_key,
    _read_back,
    _start_echo,
    _stop_echo,
    _swipe_pager,
)
from tests.e2e_ui.shells.test_terminal_ime_composition import _capture_attach_frames

pytestmark = pytest.mark.timeout(240)
_PHONE = {"width": 390, "height": 844}
_MENU = re.compile(r"/(?:help|model|compact|clear|new|settings)\b", re.I)


def _opt_in() -> None:
    if os.environ.get("OMNIGENT_TERMINAL_PROOF") != "1":
        pytest.skip("real terminal proof is opt-in; use the isolated rerun script")
    if not shutil.which("tmux"):
        pytest.skip("BLOCKED: tmux is not installed")


@contextmanager
def _phone(browser: Browser, request: pytest.FixtureRequest, **options: Any) -> Iterator[Page]:
    output = request.config.getoption("output", default=None)
    with user_page(
        browser,
        headers=options.pop("headers", {}),
        viewport=_PHONE,
        screen=_PHONE,
        is_mobile=True,
        has_touch=True,
        **(
            {"record_video_dir": str(Path(output) / "videos"), "record_video_size": _PHONE}
            if output
            else {}
        ),
        **options,
    ) as page:
        yield page


def _open_terminal(
    page: Page, base_url: str, session_id: str, terminal_id: str, *, shell: bool = False
) -> Locator:
    # Restore the selection before React can persist its default over it.
    page.add_init_script(
        """(() => {
          const {sid, id} = """
        + json.dumps({"sid": session_id, "id": terminal_id})
        + """;
          localStorage.setItem('omnigent.web.workspace-open-default', 'false');
          sessionStorage.setItem(`omnigent.web.panel-key:${sid}`, `terminal:${id}`);
        })();""",
    )
    page.goto(f"{base_url}/c/{session_id}?view=terminal")
    expect(page.locator(".app-shell")).to_be_visible(timeout=30_000)
    sidebar = page.get_by_role("button", name="Close sidebar", exact=True)
    if sidebar.is_visible():
        sidebar.click()
    if shell:
        page.get_by_role("button", name="Conversation actions", exact=True).click()
        page.get_by_role("menuitem", name=re.compile(r"^Shells\b")).click()
        drawer = page.get_by_test_id("shells-panel-drawer")
        expect(drawer).to_be_visible()
        resource = next(
            row for row in _resources(base_url, session_id) if row["id"] == terminal_id
        )
        drawer.get_by_role(
            "button", name=re.compile(re.escape(resource["metadata"]["session_key"]))
        ).first.click()
    view = page.locator(f'[data-testid="terminal-view"][data-terminal-id="{terminal_id}"]:visible')
    expect(view).to_be_visible(timeout=60_000)
    expect(view).to_have_attribute("data-state", "connected", timeout=60_000)
    return view


def _resources(base_url: str, session_id: str) -> list[dict[str, Any]]:
    response = httpx.get(f"{base_url}/v1/sessions/{session_id}/resources/terminals", timeout=10)
    response.raise_for_status()
    return response.json()["data"]


def _pane(base_url: str, session_id: str, terminal_id: str, *, styled: bool = False) -> str:
    resource = next(row for row in _resources(base_url, session_id) if row["id"] == terminal_id)
    metadata = resource["metadata"]
    args = ["tmux", "-S", metadata["tmux_socket"], "capture-pane", "-p"]
    if styled:
        args.append("-e")
    args.extend(["-t", metadata["tmux_target"]])
    return subprocess.check_output(args, text=True, timeout=5)


def _wait_screen(page: Page, read: Callable[[], str], predicate: Callable[[str], bool]) -> str:
    last = ""

    def observed() -> bool:
        nonlocal last
        page.wait_for_timeout(40)
        last = read()
        return predicate(last)

    poll_until(observed, timeout_s=15, interval_s=0.04)
    return last


def _byte_matrix(page: Page, view: Locator, root: Path) -> None:
    script = root / "keyecho.py"
    script.write_text(_KEYECHO_SOURCE, encoding="utf-8")
    textarea = view.locator("textarea.xterm-helper-textarea")
    bar = view.get_by_test_id("terminal-key-row")
    pager = bar.locator('[data-pager-row="bottom"]')
    expect(bar).to_be_visible()
    for mode in ("normal", "app"):
        log = root / f"{mode}.log"
        _start_echo(page, textarea, script, log, "app" if mode == "app" else "")
        try:
            for index, keys in enumerate((_PAGE_0_KEYS, _PAGE_1_KEYS)):
                if index:
                    _swipe_pager(pager, -80)
                expect(bar).to_have_attribute("data-bottom-page", str(index))
                for label, physical, normal, app in keys:
                    textarea.focus()
                    count = len(_lines(log))
                    page.keyboard.press(physical)
                    physical_bytes = _read_back(page, log, count)
                    textarea.evaluate("el => el.blur()")
                    count = len(_lines(log))
                    _press_soft_key(bar.get_by_role("button", name=label, exact=True))
                    _assert_key_bytes(
                        mode,
                        label,
                        _read_back(page, log, count),
                        physical_bytes,
                        app if mode == "app" else normal,
                    )
                    assert not textarea.evaluate("el => el === document.activeElement")
            _swipe_pager(pager, 80)
        finally:
            _stop_echo(page, textarea)


@contextmanager
def _other_native(
    request: pytest.FixtureRequest, family: str, root: Path
) -> Iterator[tuple[str, str]]:
    from tests.e2e_ui.conftest import _ensure_runner_online

    base_url = request.getfixturevalue("live_server")
    temporary_runner = _ensure_runner_online(base_url, request.getfixturevalue("tmp_path_factory"))
    from omnigent.server.permission_tiers import (
        PERMISSION_PROTOCOL_HEADER,
        PERMISSION_PROTOCOL_VERSION,
    )

    headers = {PERMISSION_PROTOCOL_HEADER: PERMISSION_PROTOCOL_VERSION}
    if family == "antigravity":
        from tests._helpers.native_session import create_native_session

        created = create_native_session(httpx, base_url, harness="antigravity", headers=headers)
    else:
        module = importlib.import_module(f"omnigent.harnesses.{family}_native.main")
        spec = getattr(module, f"_materialize_{family}_agent_spec")(root)
        labels = importlib.import_module("omnigent._wrapper_labels")
        wrapper = getattr(labels, f"{family.upper()}_NATIVE_WRAPPER_VALUE")
        created_response = post_session_bundle(
            httpx.post,
            f"{base_url}/v1/sessions",
            bundle_files({spec.name: spec.read_bytes()}),
            metadata={"labels": {"omnigent.ui": "terminal", "omnigent.wrapper": wrapper}},
            headers=headers,
            timeout=30,
        )
        created_response.raise_for_status()
        created = created_response.json()
    session_id = created["session_id"]
    try:
        bind_session_runner(
            httpx.patch,
            base_url,
            session_id,
            _server_state["runner_id"],
            headers=headers,
            timeout=60,
        )
        yield base_url, session_id
    finally:
        httpx.delete(
            f"{base_url}/v1/sessions/{session_id}", headers=headers, timeout=30
        ).raise_for_status()
        if temporary_runner is not None:
            from tests._helpers.live_server import terminate_process

            terminate_process(temporary_runner)


@pytest.mark.parametrize(
    "family,executable",
    [
        ("claude", "claude"),
        ("codex", "codex"),
        ("opencode", "opencode"),
        ("pi", "pi"),
        ("antigravity", "agy"),
    ],
)
def test_native_phone_menu_keys(
    request: pytest.FixtureRequest,
    browser: Browser,
    tmp_path: Path,
    family: str,
    executable: str,
    record_property: Callable[[str, object], None],
) -> None:
    """A real CLI opens a slash menu, moves its selection and dismisses it."""
    _opt_in()
    record_property("proof_category", family)
    record_property(
        "proof_limit",
        "CLI menu opened and screen changes were observed; selected-command and mode changes "
        "were not independently identified",
    )
    if not shutil.which(executable):
        pytest.skip(f"NOT RUN: {executable} is not installed")
    if family == "opencode":
        from omnigent.harnesses.opencode_native.app_server import (
            OpenCodeVersionError,
            check_opencode_version,
            resolve_opencode_version,
        )

        try:
            check_opencode_version(resolve_opencode_version(executable))
        except OpenCodeVersionError as exc:
            pytest.skip(f"BLOCKED: {exc}")
    session_context = (
        nullcontext(request.getfixturevalue(f"native_{family}_mock_session"))
        if family in {"claude", "codex"}
        else _other_native(request, family, tmp_path)
    )
    with _phone(browser, request) as page:
        with session_context as session:
            base_url, session_id = session
            terminal_id = f"terminal_{family}_main"
            if family in {"pi", "antigravity"}:
                # Retain the observed inventory without inferring a startup cause.
                inventory = _resources(base_url, session_id)
                (tmp_path / f"{family}-startup.json").write_text(
                    json.dumps({"terminals": [row["id"] for row in inventory]}),
                    encoding="utf-8",
                )
                if not any(row["id"] == terminal_id for row in inventory):
                    pytest.skip(
                        f"UNVERIFIED: isolated {family} startup produced no terminal; "
                        "startup cause is undetermined; no retained error establishes "
                        "an environment or product cause"
                    )
            view = _open_terminal(page, base_url, session_id, terminal_id)
            bar = view.get_by_test_id("terminal-key-row")
            expect(bar).to_be_visible()

            def read() -> str:
                return _pane(base_url, session_id, terminal_id, styled=True)

            _wait_screen(page, read, lambda screen: len(screen.strip()) > 20)
            before = read()
            _press_soft_key(bar.get_by_role("button", name="/", exact=True))
            menu = _wait_screen(page, read, lambda screen: bool(_MENU.search(screen)))
            assert menu != before, "slash key did not open the CLI's own command menu"
            _press_soft_key(bar.get_by_role("button", name="↓", exact=True))
            moved = _wait_screen(
                page, read, lambda screen: screen != menu and bool(_MENU.search(screen))
            )
            assert moved != menu, "Down did not change the rendered menu selection"
            _press_soft_key(bar.get_by_role("button", name="Esc", exact=True))
            escaped = _wait_screen(
                page,
                read,
                lambda screen: (
                    screen != moved and len(_MENU.findall(screen)) <= len(_MENU.findall(before))
                ),
            )
            (tmp_path / f"{family}-menu.txt").write_text(
                "BEFORE\n"
                + before
                + "\nMENU\n"
                + menu
                + "\nMOVED\n"
                + moved
                + "\nESCAPED\n"
                + escaped,
                encoding="utf-8",
            )
            if family in {"claude", "codex"}:
                # A local mode change must be visible; no prompt is submitted.
                view.locator("textarea.xterm-helper-textarea").press("Control+u")
                before_mode = read()
                _press_soft_key(bar.get_by_role("button", name="⇧Tab", exact=True))
                _wait_screen(page, read, lambda screen: screen != before_mode)


def test_declared_terminal_phone_bytes(
    request: pytest.FixtureRequest,
    browser: Browser,
    tmp_path: Path,
    record_property: Callable[[str, object], None],
) -> None:
    """The declared-terminal resource accepts every physical/soft key in both modes."""
    _opt_in()
    record_property("proof_category", "declared")
    base_url, session_id = request.getfixturevalue("terminal_session")
    response = httpx.post(
        f"{base_url}/v1/sessions/{session_id}/resources/terminals",
        json={"terminal": "zsh", "session_key": "declared-proof"},
        timeout=30,
    )
    response.raise_for_status()
    with _phone(browser, request) as page:
        view = _open_terminal(page, base_url, session_id, response.json()["id"], shell=True)
        _byte_matrix(page, view, tmp_path)


def test_sdk_tui_phone_transport_bytes(
    request: pytest.FixtureRequest,
    browser: Browser,
    record_property: Callable[[str, object], None],
) -> None:
    """Compare outgoing frames on the real SDK TUI; this is not PTY-reader proof."""
    _opt_in()
    record_property("proof_category", "sdk_tui")
    record_property(
        "proof_limit",
        "browser-to-runner frames only; Enter submits and Ctrl+C exits the SDK TUI, so both "
        "are excluded; SDK PTY-reader bytes remain unverified",
    )
    base_url, session_id = request.getfixturevalue("seeded_session")
    with _phone(browser, request) as page:
        sent, _received = _capture_attach_frames(page)
        view = _open_terminal(page, base_url, session_id, "terminal_tui_main")
        bar = view.get_by_test_id("terminal-key-row")
        textarea = view.locator("textarea.xterm-helper-textarea")

        def wait_input(after: int) -> None:
            def arrived() -> bool:
                page.wait_for_timeout(20)
                return len(sent) > after

            poll_until(arrived, timeout_s=5)

        for label, physical, normal, app in _PAGE_0_KEYS:
            if label in {"↵", "^C"}:
                continue  # Enter starts a turn; Ctrl+C exits this SDK TUI.
            textarea.focus()
            count = len(sent)
            page.keyboard.press(physical)
            wait_input(count)
            physical_bytes = sent[-1].hex()
            assert physical_bytes in {normal, app}
            textarea.evaluate("el => el.blur()")
            count = len(sent)
            _press_soft_key(bar.get_by_role("button", name=label, exact=True))
            wait_input(count)
            _assert_key_bytes(
                "SDK transport", label, sent[-1].hex(), physical_bytes, physical_bytes
            )


@pytest.mark.parametrize("elevated", [False, True], ids=["shell", "admin"])
def test_real_host_phone_bytes(
    request: pytest.FixtureRequest,
    browser: Browser,
    tmp_path: Path,
    elevated: bool,
    record_property: Callable[[str, object], None],
) -> None:
    """Drive a dedicated host process; admin stays blocked without an existing capability."""
    _opt_in()
    record_property("proof_category", "host_admin" if elevated else "host_shell")
    if elevated:
        pytest.skip(
            "BLOCKED: this proof creates no admin capability and supplies no sudo credentials"
        )
    base_url = request.getfixturevalue("live_server")
    with httpx.Client(base_url=base_url, timeout=30) as client:
        with isolated_host(
            tmp_path / "host",
            base_url,
            env={
                "OMNIGENT_RUNNER_HOST_OWNS_GLOBAL_CLEANUP": "1",
            },
        ) as host:
            host.wait_online(client)
            collection = f"/v1/hosts/{host.host_id}/terminals"
            created = client.post(collection, json={"elevated": False})
            created.raise_for_status()
            terminal_id = created.json()["terminal_id"]
            try:
                with _phone(browser, request) as page:
                    page.goto(f"{base_url}/hosts/{host.host_id}/terminals")
                    view = page.get_by_test_id("terminal-view")
                    expect(view).to_have_attribute("data-state", "connected", timeout=60_000)
                    _byte_matrix(page, view, tmp_path)
            finally:
                client.delete(f"{collection}/{terminal_id}").raise_for_status()


def test_reader_has_chat_wheel_and_no_terminal_keys(
    request: pytest.FixtureRequest,
    browser: Browser,
    record_property: Callable[[str, object], None],
) -> None:
    """An actual level-one reader sees no writable terminal bar."""
    _opt_in()
    record_property("proof_category", "read_only")
    base_url, session_id = request.getfixturevalue("terminal_session")
    reader = f"reader-{uuid.uuid4().hex}@example.test"
    httpx.put(
        f"{base_url}/v1/sessions/{session_id}/permissions",
        json={"user_id": reader, "level": 1},
        timeout=10,
    ).raise_for_status()
    response = httpx.post(
        f"{base_url}/v1/sessions/{session_id}/resources/terminals",
        json={"terminal": "zsh", "session_key": "reader-proof"},
        timeout=30,
    )
    response.raise_for_status()
    with _phone(browser, request, headers={"X-Forwarded-Email": reader}) as page:
        _open_terminal(page, base_url, session_id, "terminal_tui_main")
        expect(page.get_by_test_id("terminal-key-row")).to_have_count(0)
        wheel = page.get_by_test_id("mobile-floating-assistant")
        expect(wheel).to_be_visible()
        expect(wheel.get_by_role("button", name="Esc", exact=True)).to_have_count(0)


def test_agent_can_use_plain_user_shell_without_socket_disclosure(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    record_property: Callable[[str, object], None],
) -> None:
    """Plain shell tools follow the later host-shell contract; admin grants are separate."""
    import shlex

    from tests._helpers.session import send_user_text

    _opt_in()
    record_property("proof_category", "agent_user_shell")
    base_url, session_id = request.getfixturevalue("terminal_session")
    model_url = request.getfixturevalue("mock_llm_server_url")
    target = {"terminal": "zsh", "session": "user-proof"}
    marker = tmp_path / "agent-typed.marker"
    with httpx.Client(base_url=base_url, timeout=30) as client:
        created = client.post(
            f"/v1/sessions/{session_id}/resources/terminals",
            json={
                "terminal": "zsh",
                "session_key": target["session"],
            },
        )
        created.raise_for_status()
        reset_mock_llm(model_url)
        configure_mock_llm(
            model_url, [{"text": "first-turn-ready"}], key="first-proof", match="first-proof"
        )
        send_user_text(client, session_id=session_id, text="first-proof")

        def items() -> list[dict[str, Any]]:
            response = client.get(
                f"/v1/sessions/{session_id}/items", params={"order": "asc", "limit": 1000}
            )
            response.raise_for_status()
            return response.json()["data"]

        poll_until(lambda: "first-turn-ready" in json.dumps(items()), timeout_s=60)
        first_requests = httpx.get(f"{model_url}/mock/requests", timeout=10).json()["requests"]
        assert target["session"] not in json.dumps(first_requests)
        reset_mock_llm(model_url)
        operations = [
            ("list", {}),
            ("read", target),
            (
                "send",
                {
                    **target,
                    "text": f"printf proof > {shlex.quote(str(marker))}",
                    "keys": "Enter",
                },
            ),
        ]
        configure_mock_llm(
            model_url,
            [
                {
                    "tool_calls": [
                        {
                            "call_id": f"phone_{name}",
                            "name": f"sys_terminal_{name}",
                            "arguments": json.dumps(args),
                        }
                    ]
                }
                for name, args in operations
            ]
            + [{"text": "tool-proof-ready"}],
            key="tool-proof",
            match="tool-proof",
        )
        send_user_text(client, session_id=session_id, text="tool-proof")
        poll_until(lambda: "tool-proof-ready" in json.dumps(items()), timeout_s=90)
        poll_until(marker.exists, timeout_s=10)
        reset_mock_llm(model_url)
        configure_mock_llm(
            model_url,
            [
                {
                    "tool_calls": [
                        {
                            "call_id": "phone_close",
                            "name": "sys_terminal_close",
                            "arguments": json.dumps(target),
                        }
                    ]
                },
                {"text": "close-proof-ready"},
            ],
            key="close-proof",
            match="close-proof",
        )
        send_user_text(client, session_id=session_id, text="close-proof")
        poll_until(lambda: "close-proof-ready" in json.dumps(items()), timeout_s=60)
        outputs = {}
        for item in items():
            data = {**item, **(item.get("data") or {})}
            if item["type"] == "function_call_output" and str(data.get("call_id", "")).startswith(
                "phone_"
            ):
                outputs[data["call_id"].removeprefix("phone_")] = json.loads(data["output"])
        (tmp_path / "agent-user-shell.json").write_text(
            json.dumps(outputs, indent=2), encoding="utf-8"
        )
        assert "error" not in outputs["read"]
        entry = next(row for row in outputs["list"] if row["session"] == target["session"])
        assert entry["origin"] == "user" and "tmux_socket" not in entry
        assert outputs["send"] == {"status": "sent"}
        assert marker.read_text(encoding="utf-8") == "proof"
        assert outputs["close"]["status"] == "closed"
        assert all(row["id"] != created.json()["id"] for row in _resources(base_url, session_id))
