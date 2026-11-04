"""Mobile terminal two-row key bar byte delivery, app-cursor mode, and interaction E2E tests."""

from __future__ import annotations

import os
import re
import subprocess
import textwrap
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from playwright.sync_api import Browser, Locator, Page, Route, expect

_KEYECHO_SOURCE = textwrap.dedent(
    """\
    import os
    import pathlib
    import select
    import sys
    import termios
    import tty

    out = pathlib.Path(sys.argv[1])
    app = "app" in sys.argv[2:]
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        if app:
            os.write(1, b"\\x1b[?1h")
        out.write_text("READY\\n")
        buf = b""
        while True:
            r, _, _ = select.select([fd], [], [], 20.0)
            if not r:
                break
            chunk = os.read(fd, 64)
            if not chunk:
                break
            buf += chunk
            if b"QUITKEYECHO" in buf:
                break
            with out.open("a") as fh:
                fh.write(chunk.hex() + "\\n")
    finally:
        if app:
            os.write(1, b"\\x1b[?1l")
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
    """
)

# (aria_label, page_index, physical_key, expected_normal_hex, expected_app_hex)
_PAGE_0_KEYS: list[tuple[str, str, str, str]] = [
    ("Esc", "Escape", "1b", "1b"),
    ("↑", "ArrowUp", "1b5b41", "1b4f41"),
    ("↓", "ArrowDown", "1b5b42", "1b4f42"),
    ("↵", "Enter", "0d", "0d"),
    ("^C", "Control+c", "03", "03"),
    ("Tab", "Tab", "09", "09"),
    ("⇧Tab", "Shift+Tab", "1b5b5a", "1b5b5a"),
    ("←", "ArrowLeft", "1b5b44", "1b4f44"),
    ("→", "ArrowRight", "1b5b43", "1b4f43"),
    ("/", "/", "2f", "2f"),
    ("PgUp", "PageUp", "1b5b357e", "1b5b357e"),
]

_PAGE_1_KEYS: list[tuple[str, str, str, str]] = [
    ("PgDn", "PageDown", "1b5b367e", "1b5b367e"),
    ("Home", "Home", "1b5b48", "1b4f48"),
    ("End", "End", "1b5b46", "1b4f46"),
    ("^D", "Control+d", "04", "04"),
]


def _lines(log: Path) -> list[str]:
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


def _read_back(page: Page, log: Path, count: int, timeout_s: float = 4.0) -> str | None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        cur = _lines(log)
        if len(cur) > count:
            page.wait_for_timeout(120)
            return "".join(_lines(log)[count:])
        page.wait_for_timeout(25)
    return None


def _start_echo(page: Page, textarea: Locator, script: Path, log: Path, mode: str = "") -> None:
    if log.exists():
        log.unlink()
    textarea.focus()
    cmd = f"python3 {script} {log} {mode}".strip()
    page.keyboard.type(cmd)
    page.keyboard.press("Enter")
    deadline = time.monotonic() + 15.0
    while "READY" not in _lines(log) and time.monotonic() < deadline:
        page.wait_for_timeout(50)
    assert "READY" in _lines(log), f"keyecho did not start (log={_lines(log)})"
    page.wait_for_timeout(150)


def _stop_echo(page: Page, textarea: Locator) -> None:
    textarea.focus()
    page.keyboard.type("QUITKEYECHO")
    page.wait_for_timeout(250)


def _tap_soft_key(button: Locator) -> None:
    box = button.bounding_box()
    assert box is not None, "button has no bounding box"
    cx = box["x"] + box["width"] / 2
    cy = box["y"] + box["height"] / 2
    button.dispatch_event(
        "pointerdown",
        {
            "button": 0,
            "pointerId": 1,
            "pointerType": "touch",
            "isPrimary": True,
            "clientX": cx,
            "clientY": cy,
            "bubbles": True,
        },
    )
    button.dispatch_event(
        "pointerup",
        {
            "button": 0,
            "pointerId": 1,
            "pointerType": "touch",
            "isPrimary": True,
            "clientX": cx,
            "clientY": cy,
            "bubbles": True,
        },
    )


def _swipe_pager(pager: Locator, dx: float) -> None:
    box = pager.bounding_box()
    assert box is not None, "pager has no bounding box"
    start_x = box["x"] + box["width"] / 2
    start_y = box["y"] + box["height"] / 2
    pager.dispatch_event(
        "pointerdown",
        {
            "button": 0,
            "pointerId": 2,
            "pointerType": "touch",
            "isPrimary": True,
            "clientX": start_x,
            "clientY": start_y,
            "bubbles": True,
        },
    )
    pager.dispatch_event(
        "pointermove",
        {
            "button": 0,
            "pointerId": 2,
            "pointerType": "touch",
            "isPrimary": True,
            "clientX": start_x + dx,
            "clientY": start_y + 2,
            "bubbles": True,
        },
    )
    pager.dispatch_event(
        "pointerup",
        {
            "button": 0,
            "pointerId": 2,
            "pointerType": "touch",
            "isPrimary": True,
            "clientX": start_x + dx,
            "clientY": start_y + 2,
            "bubbles": True,
        },
    )


def _terminal_screen_text(page: Page) -> str:
    return str(
        page.evaluate(
            """() => {
              const rows = Array.from(document.querySelectorAll('.xterm-rows > div'));
              return rows.map((row) => row.textContent || '').join('\\n');
            }"""
        )
    )


def _assert_key_bytes(
    mode_name: str,
    label: str,
    by_soft: str | None,
    by_phys: str | None,
    expected: str,
) -> None:
    assert by_soft == expected, f"{mode_name} {label}: got {by_soft!r}, expected {expected!r}"
    assert by_soft == by_phys, f"{mode_name} {label}: soft {by_soft!r} != physical {by_phys!r}"


def test_mobile_terminal_keys_byte_delivery_app_cursor_and_gestures(
    browser: Browser,
    terminal_session: tuple[str, str],
    tmp_path: Path,
    request: pytest.FixtureRequest,
) -> None:
    """Verify every key on both pages in zsh/bash modes, vim/less, and gestures."""
    base_url, session_id = terminal_session
    keyecho_script = tmp_path / "keyecho.py"
    keyecho_script.write_text(_KEYECHO_SOURCE, encoding="utf-8")
    less_sample = tmp_path / "sample.txt"
    less_sample.write_text(
        "\n".join(f"LINE_{idx:03d}" for idx in range(1, 120)) + "\n",
        encoding="utf-8",
    )
    vim_file = tmp_path / "vim_out.txt"

    resp = httpx.post(
        f"{base_url}/v1/sessions/{session_id}/resources/terminals",
        json={"terminal": "zsh", "session_key": "keys1"},
        timeout=30.0,
    )
    assert resp.status_code < 300, resp.text

    configured_video_dir = os.environ.get(
        "OMNIGENT_TERMINAL_KEYS_VIDEO_DIR"
    ) or request.config.getoption("output", default=None)
    record_dir = Path(configured_video_dir) if configured_video_dir else (tmp_path / "videos")
    record_dir.mkdir(parents=True, exist_ok=True)

    context = browser.new_context(
        viewport={"width": 390, "height": 844},
        device_scale_factor=2,
        is_mobile=True,
        has_touch=True,
        record_video_dir=str(record_dir),
    )

    def handle_session_get(route: Route) -> None:
        if route.request.method != "GET":
            route.continue_()
            return
        r = route.fetch()
        if not r.ok:
            route.fulfill(response=r)
            return
        data: dict[str, Any] = r.json()
        labels = dict(data.get("labels") or {})
        labels["omnigent.ui"] = "terminal"
        data["labels"] = labels
        route.fulfill(response=r, json=data)

    def handle_conversations_get(route: Route) -> None:
        if route.request.method != "GET":
            route.continue_()
            return
        r = route.fetch()
        if not r.ok:
            route.fulfill(response=r)
            return
        data: dict[str, Any] = r.json()
        items = data.get("data")
        if isinstance(items, list):
            for row in items:
                if isinstance(row, dict) and row.get("id") == session_id:
                    labels = dict(row.get("labels") or {})
                    labels["omnigent.ui"] = "terminal"
                    row["labels"] = labels
        route.fulfill(response=r, json=data)

    context.route(re.compile(rf"/v1/sessions/{re.escape(session_id)}(?:\?|$)"), handle_session_get)
    context.route(re.compile(r"/v1/conversations(?:\?|$)"), handle_conversations_get)

    try:
        page = context.new_page()
        page.goto(f"{base_url}/c/{session_id}")
        shell_el = page.locator(".app-shell")
        expect(shell_el).to_be_visible(timeout=30_000)
        if shell_el.get_attribute("data-sidebar-open") == "true":
            close_btn = page.get_by_role("button", name="Close sidebar", exact=True)
            if close_btn.is_visible():
                close_btn.click()

        actions_btn = page.get_by_role("button", name="Conversation actions")
        expect(actions_btn).to_be_visible(timeout=30_000)
        actions_btn.click()
        shells_item = page.get_by_role("menuitem", name=re.compile(r"^Shells\b"))
        expect(shells_item).to_be_visible(timeout=15_000)
        shells_item.click()
        drawer = page.get_by_test_id("shells-panel-drawer")
        expect(drawer).to_have_attribute("data-state", "open", timeout=15_000)
        shell_row = drawer.locator("button").filter(has_text="keys1").first
        expect(shell_row).to_be_visible(timeout=15_000)
        shell_row.click()

        view = page.locator(
            '[data-testid="terminal-view"][data-terminal-id="terminal_zsh_keys1"]:visible'
        ).first
        expect(view).to_be_visible(timeout=30_000)
        expect(view).to_have_attribute("data-state", "connected", timeout=30_000)

        bar = view.locator('[data-testid="terminal-key-row"]')
        expect(bar).to_be_visible(timeout=15_000)
        expect(bar).to_have_attribute("aria-disabled", "false")
        expect(bar).to_have_attribute("data-page", "0")
        expect(bar).to_have_attribute("data-page-count", "2")

        # Floating assistant wheel must hide while a writable terminal key bar is visible.
        expect(page.get_by_test_id("mobile-floating-assistant")).to_have_count(0)

        textarea = view.locator("textarea.xterm-helper-textarea")

        # 1. Normal cursor mode in zsh: compare physical vs soft-key bytes on Page 0 & Page 1.
        normal_log = tmp_path / "zsh-normal.log"
        _start_echo(page, textarea, keyecho_script, normal_log)
        textarea.evaluate("el => el.blur()")

        for label, physical, expected_normal, _ in _PAGE_0_KEYS:
            textarea.focus()
            c0 = len(_lines(normal_log))
            page.keyboard.press(physical)
            by_phys = _read_back(page, normal_log, c0)
            textarea.evaluate("el => el.blur()")

            c1 = len(_lines(normal_log))
            _tap_soft_key(bar.get_by_role("button", name=label, exact=True))
            by_soft = _read_back(page, normal_log, c1)
            _assert_key_bytes("Normal mode", label, by_soft, by_phys, expected_normal)
            assert not page.evaluate(
                "() => document.activeElement?.classList?.contains('xterm-helper-textarea')"
                " ?? false"
            ), f"Soft key {label} must not focus xterm-helper-textarea"

        # Negative / red-proof check (§4.2): tapping a deliberately wrong key (End instead of Home,
        # or comparing against application-cursor Home 1b4f48) fails _assert_key_bytes.
        pager = bar.locator('[data-testid="terminal-key-row-pager"]')
        _swipe_pager(pager, -80)
        expect(bar).to_have_attribute("data-page", "1")

        c_wrong = len(_lines(normal_log))
        _tap_soft_key(bar.get_by_role("button", name="End", exact=True))
        wrong_soft = _read_back(page, normal_log, c_wrong)
        with pytest.raises(AssertionError):
            _assert_key_bytes("Normal mode", "Home", wrong_soft, "1b5b48", "1b5b48")

        for label, physical, expected_normal, expected_app in _PAGE_1_KEYS:
            textarea.focus()
            c0 = len(_lines(normal_log))
            page.keyboard.press(physical)
            by_phys = _read_back(page, normal_log, c0)
            textarea.evaluate("el => el.blur()")

            c1 = len(_lines(normal_log))
            _tap_soft_key(bar.get_by_role("button", name=label, exact=True))
            by_soft = _read_back(page, normal_log, c1)
            _assert_key_bytes("Normal mode", label, by_soft, by_phys, expected_normal)
            if label in ("Home", "End"):
                with pytest.raises(AssertionError):
                    _assert_key_bytes("Normal mode", label, by_soft, expected_app, expected_app)

        # Swipe right > 36px flips back to Page 0 without firing a key.
        before_swipe_lines = len(_lines(normal_log))
        _swipe_pager(pager, 80)
        expect(bar).to_have_attribute("data-page", "0")
        page.wait_for_timeout(120)
        assert len(_lines(normal_log)) == before_swipe_lines, "Pager swipe must not emit key bytes"

        # Drag >= 10px on a soft key cancels the press without emitting bytes.
        slash_btn = bar.get_by_role("button", name="/", exact=True)
        sbox = slash_btn.bounding_box()
        assert sbox is not None
        before_drag_lines = len(_lines(normal_log))
        slash_btn.dispatch_event(
            "pointerdown",
            {
                "button": 0,
                "pointerId": 4,
                "pointerType": "touch",
                "isPrimary": True,
                "clientX": sbox["x"] + 10,
                "clientY": sbox["y"] + 10,
                "bubbles": True,
            },
        )
        slash_btn.dispatch_event(
            "pointermove",
            {
                "button": 0,
                "pointerId": 4,
                "pointerType": "touch",
                "isPrimary": True,
                "clientX": sbox["x"] + 30,
                "clientY": sbox["y"] + 10,
                "bubbles": True,
            },
        )
        slash_btn.dispatch_event(
            "pointerup",
            {
                "button": 0,
                "pointerId": 4,
                "pointerType": "touch",
                "isPrimary": True,
                "clientX": sbox["x"] + 30,
                "clientY": sbox["y"] + 10,
                "bubbles": True,
            },
        )
        page.wait_for_timeout(150)
        assert len(_lines(normal_log)) == before_drag_lines, (
            "Cancelled drag must not emit key bytes"
        )

        # Hold-to-repeat on arrow key (400ms initial delay + 80ms interval).
        up_btn = bar.get_by_role("button", name="↑", exact=True)
        ubox = up_btn.bounding_box()
        assert ubox is not None
        before_repeat = len(_lines(normal_log))
        up_btn.dispatch_event(
            "pointerdown",
            {
                "button": 0,
                "pointerId": 5,
                "pointerType": "touch",
                "isPrimary": True,
                "clientX": ubox["x"] + 15,
                "clientY": ubox["y"] + 15,
                "bubbles": True,
            },
        )
        repeat_deadline = time.monotonic() + 3.0
        repeat_hex = ""
        repeat_count = 0
        while time.monotonic() < repeat_deadline:
            repeat_hex = "".join(_lines(normal_log)[before_repeat:])
            repeat_count = repeat_hex.count("1b5b41")
            if repeat_count >= 2:
                break
            page.wait_for_timeout(50)
        up_btn.dispatch_event(
            "pointerup",
            {
                "button": 0,
                "pointerId": 5,
                "pointerType": "touch",
                "isPrimary": True,
                "clientX": ubox["x"] + 15,
                "clientY": ubox["y"] + 15,
                "bubbles": True,
            },
        )
        assert repeat_count >= 2, (
            f"Expected hold-to-repeat >=2 sequences, got {repeat_count} ({repeat_hex!r})"
        )

        _stop_echo(page, textarea)

        # 2. Application cursor mode (\x1b[?1h) in bash: verify all 15 byte-producing keys.
        textarea.focus()
        page.keyboard.type("bash\n")
        page.wait_for_timeout(250)
        app_log = tmp_path / "bash-app.log"
        _start_echo(page, textarea, keyecho_script, app_log, "app")
        textarea.evaluate("el => el.blur()")

        for label, physical, _, expected_app in _PAGE_0_KEYS:
            textarea.focus()
            c0 = len(_lines(app_log))
            page.keyboard.press(physical)
            by_phys = _read_back(page, app_log, c0)
            textarea.evaluate("el => el.blur()")

            c1 = len(_lines(app_log))
            _tap_soft_key(bar.get_by_role("button", name=label, exact=True))
            by_soft = _read_back(page, app_log, c1)
            _assert_key_bytes("App mode", label, by_soft, by_phys, expected_app)

        _swipe_pager(pager, -80)
        expect(bar).to_have_attribute("data-page", "1")
        for label, physical, _, expected_app in _PAGE_1_KEYS:
            textarea.focus()
            c0 = len(_lines(app_log))
            page.keyboard.press(physical)
            by_phys = _read_back(page, app_log, c0)
            textarea.evaluate("el => el.blur()")

            c1 = len(_lines(app_log))
            _tap_soft_key(bar.get_by_role("button", name=label, exact=True))
            by_soft = _read_back(page, app_log, c1)
            _assert_key_bytes("App mode", label, by_soft, by_phys, expected_app)

        _swipe_pager(pager, 80)
        expect(bar).to_have_attribute("data-page", "0")
        _stop_echo(page, textarea)
        textarea.focus()
        page.keyboard.type("exit\n")
        page.wait_for_timeout(200)

        # 3. Real less paging with PgDn and PgUp (verified via tmux capture-pane on the live PTY).
        tmux_env_file = tmp_path / "tmux_env.txt"
        textarea.focus()
        page.keyboard.type(f'echo "$TMUX" > {tmux_env_file}\n')
        deadline = time.monotonic() + 8.0
        while not tmux_env_file.exists() and time.monotonic() < deadline:
            page.wait_for_timeout(50)
        assert tmux_env_file.exists(), "did not capture $TMUX"
        tmux_socket = tmux_env_file.read_text(encoding="utf-8").strip().split(",")[0]

        def capture_pty() -> str:
            return subprocess.check_output(
                ["tmux", "-S", tmux_socket, "capture-pane", "-p"],
                text=True,
                timeout=5.0,
            )

        textarea.focus()
        page.keyboard.type(f"less -f {less_sample}\n")
        deadline = time.monotonic() + 8.0
        while "LINE_001" not in capture_pty() and time.monotonic() < deadline:
            page.wait_for_timeout(50)
        assert "LINE_001" in capture_pty(), "less did not open sample.txt"

        textarea.evaluate("el => el.blur()")
        _swipe_pager(pager, -80)
        expect(bar).to_have_attribute("data-page", "1")
        _tap_soft_key(bar.get_by_role("button", name="PgDn", exact=True))
        deadline = time.monotonic() + 8.0
        while "LINE_040" not in capture_pty() and time.monotonic() < deadline:
            page.wait_for_timeout(50)
        assert "LINE_040" in capture_pty(), "PgDn did not advance less page"

        _swipe_pager(pager, 80)
        expect(bar).to_have_attribute("data-page", "0")
        _tap_soft_key(bar.get_by_role("button", name="PgUp", exact=True))
        deadline = time.monotonic() + 8.0
        while "LINE_001" not in capture_pty() and time.monotonic() < deadline:
            page.wait_for_timeout(50)
        assert "LINE_001" in capture_pty(), "PgUp did not return less to top"
        textarea.focus()
        page.keyboard.type("q")
        page.wait_for_timeout(200)

        # 4. Real vim insert mode exit via Esc and cursor movement via arrow keys.
        textarea.focus()
        page.keyboard.type(f"vim -u NONE -N {vim_file}\n")
        page.wait_for_timeout(500)
        page.keyboard.type("ihello")
        page.wait_for_timeout(150)
        textarea.evaluate("el => el.blur()")
        _tap_soft_key(bar.get_by_role("button", name="Esc", exact=True))
        page.wait_for_timeout(150)
        _tap_soft_key(bar.get_by_role("button", name="←", exact=True))
        page.wait_for_timeout(100)
        textarea.focus()
        page.keyboard.type("x:wq\n")
        deadline = time.monotonic() + 8.0
        while not vim_file.exists() and time.monotonic() < deadline:
            page.wait_for_timeout(50)
        assert vim_file.exists(), "vim did not write output file after Esc + arrow key"
        assert vim_file.read_text(encoding="utf-8").strip() == "helo"

        # 5. Keyboard button click behavior: blur/focus toggle based on focused + keyboardUp.
        textarea.evaluate("el => el.blur()")
        kbd_btn = bar.get_by_role("button", name="Keyboard", exact=True)
        expect(kbd_btn).to_have_attribute("aria-pressed", "false")
        kbd_btn.click()
        assert page.evaluate(
            "() => document.activeElement?.classList?.contains('xterm-helper-textarea') ?? false"
        ), "Clicking Keyboard while blurred must focus xterm-helper-textarea"

        # Simulate keyboardUp via visualViewport shrink (> 80px) and verify Keyboard click blurs.
        page.evaluate(
            """() => {
              const vv = window.visualViewport;
              if (vv) {
                Object.defineProperty(vv, "height", { configurable: true, get: () => 470 });
                vv.dispatchEvent(new Event("resize"));
              }
              window.dispatchEvent(new Event("resize"));
              return new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
            }"""
        )
        expect(view).to_have_attribute("data-keyboard-up", "true")
        expect(kbd_btn).to_have_attribute("aria-pressed", "true")
        kbd_btn.click()
        assert not page.evaluate(
            "() => document.activeElement?.classList?.contains('xterm-helper-textarea') ?? false"
        ), "Clicking Keyboard while focused and keyboardUp must blur xterm-helper-textarea"

        # 6. Closing the shell returns to chat view and restores the MobileFloatingAssistant wheel.
        close_shell = page.get_by_role("button", name="Close shell", exact=True)
        expect(close_shell).to_be_visible()
        close_shell.click()
        expect(page.get_by_test_id("mobile-floating-assistant")).to_be_visible(timeout=10_000)
    finally:
        context.close()
