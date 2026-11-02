"""E2E acceptance (§4.1): terminal key rows clearance, keyboard room, and safe areas.

Sweeps phone portrait and landscape viewports across browser, iOS shell, and Android
shell bridges, keyboard states (down, overlay 0/40, iOS bridge, layout-resize, short
44px accessory strip, and rotation), safe areas, and all five terminal surfaces.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

import httpx
import pytest
from playwright.sync_api import Browser, BrowserContext, Page, Route, WebSocketRoute, expect

PORTRAIT_MAIN = (390, 844)
LANDSCAPE_MAIN = (844, 390)
OTHER_VIEWPORTS = [
    (360, 640),
    (430, 932),
    (667, 375),
    (932, 430),
]

SURFACES = (
    "agent-cli",
    "shell-view",
    "host-terminal",
    "terminals-panel",
    "read-only",
)

SAFE_ZERO = {"top": 0, "bottom": 0, "left": 0, "right": 0}
SAFE_PORTRAIT = {"top": 47, "bottom": 34, "left": 0, "right": 0}
SAFE_LANDSCAPE = {"top": 0, "bottom": 21, "left": 47, "right": 47}

_INIT_SCRIPT_TEMPLATE = """
(() => {
  const shellKind = __SHELL_KIND__;
  let vvHeightOverride = null;
  let vvOffsetTopOverride = 0;
  let bridgeHeightOverride = null;
  const nativeListeners = new Set();

  if (window.visualViewport) {
    const proto = Object.getPrototypeOf(window.visualViewport);
    const origHeight = Object.getOwnPropertyDescriptor(proto, "height");
    const origOffsetTop = Object.getOwnPropertyDescriptor(proto, "offsetTop");
    Object.defineProperty(window.visualViewport, "height", {
      configurable: true,
      get() {
        if (vvHeightOverride !== null) return vvHeightOverride;
        return origHeight ? origHeight.get.call(this) : window.innerHeight;
      },
    });
    Object.defineProperty(window.visualViewport, "offsetTop", {
      configurable: true,
      get() {
        if (vvOffsetTopOverride !== null) return vvOffsetTopOverride;
        return origOffsetTop ? origOffsetTop.get.call(this) : 0;
      },
    });
  }

  let screenW = window.screen.width;
  let screenH = window.screen.height;
  Object.defineProperty(window.screen, "width", {
    configurable: true,
    get() { return screenW; },
  });
  Object.defineProperty(window.screen, "height", {
    configurable: true,
    get() { return screenH; },
  });

  if (shellKind === "ios") {
    window.omnigentNative = {
      kind: "ios",
      setBadgeCount: function () {},
      notify: function () { return Promise.resolve(false); },
      onNotificationActivated: function () { return function () {}; },
      onOpenPath: function () { return function () {}; },
      onNativeInsets: function () { return function () {}; },
      onSidebarDrag: function () { return function () {}; },
      onViewModeChanged: function () { return function () {}; },
      setViewMode: function () {},
      setServerSwitcherHidden: function () {},
      setSidebarOpen: function () {},
      setDocumentScrollEnabled: function () {},
      getKeyboardViewport: function () {
        const h = bridgeHeightOverride !== null ? bridgeHeightOverride : window.innerHeight;
        return { width: window.innerWidth, height: h };
      },
      onKeyboardViewportChanged: function (cb) {
        nativeListeners.add(cb);
        return function () { nativeListeners.delete(cb); };
      },
    };
  } else if (shellKind === "android") {
    window.omnigentNative = {
      kind: "android",
      setBadgeCount: function () {},
      notify: function () { return Promise.resolve(false); },
      onNotificationActivated: function () { return function () {}; },
      onNativeInsets: function () { return function () {}; },
    };
  }

  window.__setScreenSize = (w, h) => {
    screenW = w;
    screenH = h;
  };

  window.__setKeyboardState = ({ vvHeight = null, vvOffsetTop = 0, bridgeHeight = null }) => {
    vvHeightOverride = vvHeight;
    vvOffsetTopOverride = vvOffsetTop;
    bridgeHeightOverride = bridgeHeight;
    if (window.visualViewport) {
      window.visualViewport.dispatchEvent(new Event("resize"));
      if (vvOffsetTop > 0) {
        window.visualViewport.dispatchEvent(new Event("scroll"));
      }
    }
    window.dispatchEvent(new Event("resize"));
    for (const cb of [...nativeListeners]) cb();
    return new Promise((resolve) => {
      requestAnimationFrame(() => requestAnimationFrame(resolve));
    });
  };
})();
"""


def _ensure_session_terminals(base_url: str, session_id: str) -> None:
    """Create both an agent CLI terminal and a user shell on the real runner."""
    resp_cli = httpx.post(
        f"{base_url}/v1/sessions/{session_id}/resources/terminals",
        json={
            "terminal": "claude",
            "session_key": "main",
            "ensure_native_terminal": True,
            "spec": {"command": "bash", "args": ["--noprofile", "--norc"]},
        },
        timeout=20.0,
    )
    resp_cli.raise_for_status()
    resp_shell = httpx.post(
        f"{base_url}/v1/sessions/{session_id}/resources/terminals",
        json={"terminal": "zsh", "session_key": "clear1"},
        timeout=20.0,
    )
    resp_shell.raise_for_status()


def _install_surface_routes(
    context: BrowserContext,
    base_url: str,
    session_id: str,
    state: dict[str, Any],
) -> None:
    """Route host terminal endpoints and session metadata overrides per surface."""
    host_id = "test-host"
    term_id = "term_host_1"
    scrollback = "\r\n".join(f"host-scroll-line-{i}" for i in range(1, 90)).encode() + b"\r\n$ "

    def handle_hosts(route: Route) -> None:
        route.fulfill(
            json={
                "hosts": [
                    {
                        "host_id": host_id,
                        "name": "Example host",
                        "owner": "local",
                        "status": "online",
                        "capabilities": ["host_terminals_v1"],
                    }
                ]
            }
        )

    def handle_host_terminals(route: Route) -> None:
        route.fulfill(
            json={
                "terminals": [
                    {
                        "terminal_id": term_id,
                        "elevated": False,
                        "created_at": 1,
                        "attach_path": f"/v1/hosts/{host_id}/terminals/{term_id}/attach",
                    }
                ]
            }
        )

    def handle_host_ws(socket: WebSocketRoute) -> None:
        state["host_ws"] = socket
        socket.send(scrollback)

        def on_msg(msg: str | bytes) -> None:
            if isinstance(msg, bytes):
                socket.send(msg + b"\r\nhost-echo\r\n$ ")

        socket.on_message(on_msg)

    def handle_session_get(route: Route) -> None:
        if route.request.method != "GET":
            route.continue_()
            return
        resp = route.fetch()
        if not resp.ok:
            route.fulfill(response=resp)
            return
        data = resp.json()
        surface = state.get("surface", "agent-cli")
        labels = dict(data.get("labels") or {})
        labels["omnigent.ui"] = "chat" if surface == "terminals-panel" else "terminal"
        data["labels"] = labels
        if surface == "read-only":
            data["permission_level"] = 1
            data["permissionLevel"] = 1
        route.fulfill(response=resp, json=data)

    def handle_conversations_get(route: Route) -> None:
        if route.request.method != "GET":
            route.continue_()
            return
        resp = route.fetch()
        if not resp.ok:
            route.fulfill(response=resp)
            return
        data = resp.json()
        surface = state.get("surface", "agent-cli")
        items = data.get("data")
        if isinstance(items, list):
            for row in items:
                if isinstance(row, dict) and row.get("id") == session_id:
                    labels = dict(row.get("labels") or {})
                    labels["omnigent.ui"] = "chat" if surface == "terminals-panel" else "terminal"
                    row["labels"] = labels
                    if surface == "read-only":
                        row["permission_level"] = 1
        route.fulfill(response=resp, json=data)

    context.route("**/v1/hosts", handle_hosts)
    context.route(f"**/v1/hosts/{host_id}/terminals", handle_host_terminals)
    context.route_web_socket(f"**/v1/hosts/{host_id}/terminals/{term_id}/attach*", handle_host_ws)
    context.route(re.compile(rf"/v1/sessions/{re.escape(session_id)}(?:\?|$)"), handle_session_get)
    context.route(re.compile(r"/v1/conversations(?:\?|$)"), handle_conversations_get)


def _apply_safe_area(page: Page, cdp: Any, shell: str, surface: str, safe: dict[str, int]) -> None:
    if shell in ("ios", "android"):
        cdp.send("Emulation.setSafeAreaInsetsOverride", {"insets": safe})
    page.evaluate(
        """({ shell, surface, safe }) => {
          const style = document.documentElement.style;
          if (shell === "android") {
            style.setProperty("--omnigent-android-safe-area-top", `${safe.top}px`);
            style.setProperty("--omnigent-android-safe-area-bottom", `${safe.bottom}px`);
            style.setProperty("--omnigent-android-safe-area-left", `${safe.left}px`);
            style.setProperty("--omnigent-android-safe-area-right", `${safe.right}px`);
          } else if (shell === "browser") {
            const topPx = surface === "host-terminal" ? safe.top : 0;
            style.setProperty("--omnigent-safe-top", `${topPx}px`);
            style.setProperty("--omnigent-safe-bottom", `${safe.bottom}px`);
            style.setProperty("--omnigent-safe-left", `${safe.left}px`);
            style.setProperty("--omnigent-safe-right", `${safe.right}px`);
          }
          window.dispatchEvent(new Event("resize"));
          return new Promise((resolve) =>
            requestAnimationFrame(() => requestAnimationFrame(resolve)),
          );
        }""",
        {"shell": shell, "surface": surface, "safe": safe},
    )


def _open_surface(
    page: Page,
    base_url: str,
    session_id: str,
    surface: str,
    state: dict[str, Any],
    seeded_ptys: set[str],
) -> None:
    state["surface"] = surface
    w0, h0 = PORTRAIT_MAIN
    page.set_viewport_size({"width": w0, "height": h0})
    if surface == "host-terminal":
        page.goto(f"{base_url}/hosts/test-host/terminals")
        expect(page.get_by_role("heading", name="Example host · Terminals")).to_be_visible(
            timeout=30_000
        )
    elif surface == "terminals-panel":
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
        shell_row = drawer.locator("button").filter(has_text="clear1").first
        expect(shell_row).to_be_visible(timeout=15_000)
        shell_row.click()
        panel = page.get_by_test_id("terminals-panel")
        expect(panel).to_have_attribute("data-state", "open", timeout=30_000)
        expect(panel).to_have_attribute("data-expanded", "true", timeout=10_000)
    else:
        panel_key = (
            "terminal:terminal_zsh_clear1"
            if surface == "shell-view"
            else "terminal:terminal_claude_main"
        )
        page.goto(f"{base_url}/c/{session_id}?view=terminal")
        page.evaluate(
            """({ sid, key }) => {
              localStorage.setItem("omnigent.web.workspace-open-default", "false");
              localStorage.setItem(
                `omnigent.web.workspace:${sid}`,
                JSON.stringify({ open: false, tab: null, files: [], activeFile: null }),
              );
              sessionStorage.setItem(`omnigent.web.panel-key:${sid}`, key);
            }""",
            {"sid": session_id, "key": panel_key},
        )
        page.reload()

    if surface not in ("host-terminal", "terminals-panel"):
        shell_el = page.locator(".app-shell")
        expect(shell_el).to_be_visible(timeout=30_000)
        if shell_el.get_attribute("data-sidebar-open") == "true":
            close_btn = page.get_by_role("button", name="Close sidebar", exact=True)
            if close_btn.is_visible():
                close_btn.click()
        collapse_rail = page.locator('button[aria-label="Collapse right panel"]')
        if collapse_rail.count() > 0 and collapse_rail.first.is_visible():
            collapse_rail.first.click()

    view = page.locator('[data-testid="terminal-view"]:visible').first
    expect(view).to_be_visible(timeout=30_000)
    expect(view).to_have_attribute("data-state", "connected", timeout=30_000)
    _settle_sidebar_if_needed(page, surface)

    # Seed scrollback once per real PTY so scrollback assertions have >60 lines.
    pty_id = view.get_attribute("data-terminal-id") or surface
    if surface not in ("host-terminal", "read-only") and pty_id not in seeded_ptys:
        textarea = view.locator("textarea.xterm-helper-textarea")
        textarea.focus()
        page.keyboard.type("for i in $(seq 1 80); do echo line-$i; done")
        page.keyboard.press("Enter")
        page.wait_for_timeout(350)
        textarea.evaluate("el => el.blur()")
        seeded_ptys.add(pty_id)


def _settle_sidebar_if_needed(page: Page, surface: str) -> None:
    page.evaluate(
        """() => {
          if (!document.getElementById("__no_anim__")) {
            const st = document.createElement("style");
            st.id = "__no_anim__";
            st.textContent =
              "*, *::before, *::after { transition-duration: 0s !important; " +
              "animation-duration: 0s !important; }";
            document.head.appendChild(st);
          }
        }"""
    )
    if surface in ("host-terminal", "terminals-panel"):
        return
    shell_el = page.locator(".app-shell")
    if shell_el.count() and shell_el.get_attribute("data-sidebar-open") == "true":
        close_btn = page.get_by_role("button", name="Close sidebar", exact=True)
        if close_btn.is_visible():
            close_btn.click()
            page.wait_for_timeout(100)


def _measure_and_assert(
    page: Page,
    *,
    shell: str,
    surface: str,
    nominal_w: int,
    nominal_h: int,
    kb_mode: str,
    safe: dict[str, int],
    mutation: str | None = None,
) -> dict[str, Any]:
    """Configure the keyboard state and assert all §4.1 branch and geometry invariants."""
    kb_up_h = nominal_h - 125 if nominal_w > nominal_h else round(nominal_h * 0.55)
    layout_h = kb_up_h if kb_mode == "layout_resize_up" else nominal_h
    page.evaluate("({ w, h }) => window.__setScreenSize(w, h)", {"w": nominal_w, "h": nominal_h})
    current_vp = page.viewport_size
    if not current_vp or current_vp["width"] != nominal_w or current_vp["height"] != layout_h:
        page.set_viewport_size({"width": nominal_w, "height": layout_h})
        _settle_sidebar_if_needed(page, surface)

    if kb_mode == "down":
        vv_h, vv_top, bridge_h = layout_h, 0, layout_h if shell == "ios" else None
        visible_bottom = layout_h
    elif kb_mode == "overlay_up_0":
        vv_h = kb_up_h
        vv_top = 0
        bridge_h = None
        visible_bottom = vv_h
    elif kb_mode == "overlay_up_40":
        vv_h = kb_up_h
        vv_top = 40
        bridge_h = None
        visible_bottom = vv_top + vv_h
    elif kb_mode == "ios_bridge_up":
        bh = kb_up_h
        vv_h, vv_top, bridge_h = bh, 0, bh
        visible_bottom = bh
    elif kb_mode == "layout_resize_up":
        vv_h, vv_top, bridge_h = layout_h, 0, None
        visible_bottom = layout_h
    elif kb_mode == "short_strip":
        strip_h = nominal_h - 44
        vv_h = strip_h
        vv_top = 0
        bridge_h = strip_h if shell == "ios" else None
        visible_bottom = strip_h
    elif kb_mode == "room_floor":
        # Leave ~150px room above visible_bottom so portrait roomPx < 99 + 108 = 207px
        # (hiding the bar) while keeping >= 4 terminal rows (~150px / 17px >= 8 rows).
        view_top = page.evaluate(
            """() => {
              const v = [...document.querySelectorAll('[data-testid="terminal-view"]')].find(
                (el) => el.offsetParent !== null,
              );
              return v ? v.getBoundingClientRect().top : 40;
            }"""
        )
        floor_h = round(float(view_top)) + 150
        vv_h = floor_h
        vv_top = 0
        bridge_h = floor_h if shell == "ios" else None
        visible_bottom = floor_h
    else:
        raise ValueError(f"Unknown kb_mode: {kb_mode}")

    page.evaluate(
        "(cfg) => window.__setKeyboardState(cfg)",
        {"vvHeight": vv_h, "vvOffsetTop": vv_top, "bridgeHeight": bridge_h},
    )

    if mutation is not None:
        page.evaluate(
            """(mut) => {
              const view = [...document.querySelectorAll('[data-testid="terminal-view"]')].find(
                (el) => el.offsetParent !== null,
              );
              if (!view) return;
              const bar = view.querySelector('[data-testid="terminal-key-row"]');
              if (mut === "remove_root_padding") {
                view.style.setProperty("padding-bottom", "0px", "important");
              } else if (mut === "key_row_absolute" && bar) {
                bar.style.position = "absolute";
                bar.style.bottom = "0px";
                bar.style.left = "0px";
                bar.style.right = "0px";
              } else if (mut === "clearance_in_scroll") {
                const menuBtn =
                  document.querySelector('button[aria-label="Open sidebar"]') ||
                  document.querySelector(".chat-header") ||
                  document.querySelector("main header");
                if (menuBtn) menuBtn.style.transform = "translateY(-40px)";
              } else if (mut === "ios_host_overlap_zero") {
                view.setAttribute("data-keyboard-overlap", "0");
                view.style.setProperty("padding-bottom", "0px", "important");
              } else if (mut === "overlap_threshold_80") {
                view.setAttribute("data-keyboard-overlap", "0");
                view.style.setProperty("padding-bottom", "0px", "important");
              }
            }""",
            mutation,
        )

    snap = page.evaluate(
        """({ surface }) => {
          const view = [...document.querySelectorAll('[data-testid="terminal-view"]')].find(
            (el) => el.offsetParent !== null,
          );
          if (!view) throw new Error(`No visible terminal-view on ${surface}`);
          const viewRect = view.getBoundingClientRect();
          const viewStyle = getComputedStyle(view);
          const xtermWrapper = view.querySelector("div.relative.min-h-0.flex-1");
          const xtermInner =
            (xtermWrapper ? xtermWrapper.firstElementChild : null) || xtermWrapper;
          const xtermRect = xtermInner ? xtermInner.getBoundingClientRect() : viewRect;
          const bar = view.querySelector('[data-testid="terminal-key-row"]');
          const barRect = bar ? bar.getBoundingClientRect() : null;
          const buttons = bar
            ? [...bar.querySelectorAll("button")].map((b) => {
                const r = b.getBoundingClientRect();
                return {
                  label: b.getAttribute("aria-label"),
                  left: r.left,
                  top: r.top,
                  right: r.right,
                  bottom: r.bottom,
                  width: r.width,
                  height: r.height,
                };
              })
            : [];
          const navRects = [];
          if (surface === "host-terminal") {
            const hdr = document.querySelector("main header");
            if (hdr) {
              navRects.push({ name: "host-header", ...hdr.getBoundingClientRect().toJSON() });
            }
            const tabs = document.querySelector('[role="tablist"][aria-label="Host terminals"]');
            if (tabs) {
              navRects.push({ name: "host-tabs", ...tabs.getBoundingClientRect().toJSON() });
            }
          } else if (surface === "terminals-panel") {
            const hdr = document.querySelector('[data-testid="terminals-panel"] > header');
            if (hdr) {
              navRects.push({ name: "panel-header", ...hdr.getBoundingClientRect().toJSON() });
            }
          } else {
            const menuBtn =
              document.querySelector('button[aria-label="Open sidebar"]') ||
              document.querySelector(".chat-header");
            if (menuBtn) {
              navRects.push({ name: "menu-control", ...menuBtn.getBoundingClientRect().toJSON() });
            }
            if (surface === "shell-view") {
              const closeShell = document.querySelector('button[aria-label="Close shell"]');
              const shellBar = closeShell ? closeShell.parentElement : null;
              if (shellBar) {
                navRects.push({
                  name: "shell-header",
                  ...shellBar.getBoundingClientRect().toJSON(),
                });
              }
            }
          }
          const appShell = document.querySelector(".app-shell");
          const appShellRect = appShell ? appShell.getBoundingClientRect() : null;
          const rowsEl = view.querySelector(".xterm-rows");
          const rowCount = rowsEl ? rowsEl.children.length : 0;
          const firstRowEl = rowsEl ? rowsEl.firstElementChild : null;
          const firstRowH = firstRowEl ? firstRowEl.getBoundingClientRect().height : 0;
          const cellHeight = firstRowH >= 12 && firstRowH <= 28 ? firstRowH : 17;
          const visibleRows = Math.floor(xtermRect.height / cellHeight);
          const wheelCount = document.querySelectorAll(
            '[data-testid="mobile-floating-assistant"]',
          ).length;
          return {
            coarse: window.matchMedia("(pointer: coarse)").matches,
            maxMd: window.matchMedia("(max-width: 767.98px)").matches,
            screenW: window.screen.width,
            screenH: window.screen.height,
            innerW: window.innerWidth,
            innerH: window.innerHeight,
            scrollY: window.scrollY,
            iosNativeAttr: appShell ? appShell.getAttribute("data-ios-native") : null,
            androidNativeAttr: appShell ? appShell.getAttribute("data-android-native") : null,
            viewportHeightVar: document.documentElement.style
              .getPropertyValue("--omnigent-viewport-height")
              .trim(),
            appShellHeight: appShellRect ? Math.round(appShellRect.height) : null,
            overlapAttr: view.getAttribute("data-keyboard-overlap"),
            keyboardUpAttr: view.getAttribute("data-keyboard-up"),
            paddingBottomPx: parseFloat(viewStyle.paddingBottom || "0") || 0,
            viewRect: viewRect.toJSON(),
            xtermRect: xtermRect.toJSON(),
            barRect: barRect ? barRect.toJSON() : null,
            buttons,
            navRects,
            rowCount,
            visibleRows,
            wheelCount,
          };
        }""",
        {"surface": surface},
    )

    ctx_desc = (
        f"shell={shell} surface={surface} vp={nominal_w}x{nominal_h} kb={kb_mode} safe={safe}"
    )

    # --- Branch reached assertions (§4.1) ---
    assert snap["coarse"] is True, f"pointer:coarse not reached ({ctx_desc})"
    assert snap["screenW"] == nominal_w and snap["screenH"] == nominal_h, (
        f"screen size mismatch {snap['screenW']}x{snap['screenH']} ({ctx_desc})"
    )
    assert snap["maxMd"] is (nominal_w < 768), (
        f"max-md query mismatch {snap['maxMd']} ({ctx_desc})"
    )

    if shell == "ios":
        assert bridge_h is not None
        assert snap["iosNativeAttr"] == "true", f"missing data-ios-native ({ctx_desc})"
        assert snap["viewportHeightVar"] == f"{bridge_h}px", (
            f"--omnigent-viewport-height={snap['viewportHeightVar']} != {bridge_h}px ({ctx_desc})"
        )
        assert (
            snap["appShellHeight"] is not None and abs(snap["appShellHeight"] - bridge_h) <= 1
        ), f".app-shell height={snap['appShellHeight']} != {bridge_h} ({ctx_desc})"
        if surface != "read-only" and kb_mode == "ios_bridge_up":
            if surface == "host-terminal":
                expected_host_overlap = max(0, (nominal_h - bridge_h) - safe["bottom"])
                assert int(snap["overlapAttr"] or "-1") == expected_host_overlap, (
                    f"host page iOS overlap={snap['overlapAttr']} "
                    f"!= {expected_host_overlap} ({ctx_desc})"
                )
            else:
                assert snap["overlapAttr"] == "0", (
                    f"session surface iOS overlap={snap['overlapAttr']} != 0 ({ctx_desc})"
                )
    elif shell == "browser":
        assert snap["iosNativeAttr"] is None, f"unexpected data-ios-native in browser ({ctx_desc})"
        assert snap["viewportHeightVar"] == "", (
            f"unexpected --omnigent-viewport-height={snap['viewportHeightVar']}"
            f" in browser ({ctx_desc})"
        )
        if surface != "read-only" and kb_mode in ("overlay_up_0", "overlay_up_40"):
            assert int(snap["overlapAttr"] or "0") > 0, (
                f"browser overlay keyboard overlap={snap['overlapAttr']} not > 0 ({ctx_desc})"
            )

    if kb_mode == "short_strip" and surface != "read-only":
        if shell == "ios":
            expected_strip_overlap = (
                max(0, 44 - safe["bottom"]) if surface == "host-terminal" else 0
            )
        else:
            expected_strip_overlap = (
                max(0, 44 - safe["bottom"]) if surface == "host-terminal" else 44
            )
        assert int(snap["overlapAttr"] or "-1") == expected_strip_overlap, (
            f"short strip overlap={snap['overlapAttr']} != {expected_strip_overlap} ({ctx_desc})"
        )
        expected_kb_up = "true" if shell == "ios" else "false"
        assert snap["keyboardUpAttr"] == expected_kb_up, (
            f"short strip keyboardUp={snap['keyboardUpAttr']} != {expected_kb_up} ({ctx_desc})"
        )

    # --- Assertion 8: Read-only has no bar ---
    if surface == "read-only":
        assert snap["barRect"] is None, f"read-only must not render a bar ({ctx_desc})"
        assert snap["wheelCount"] > 0, f"read-only must keep chat wheel ({ctx_desc})"
        return snap

    # --- Assertion 1: Navigation bottom <= terminal viewport top, and top >= safe-area top ---
    effective_safe_top = (
        safe["top"] if (shell in ("ios", "android") or surface == "host-terminal") else 0
    )
    assert len(snap["navRects"]) > 0, f"no navigation rects found ({ctx_desc})"
    for nav in snap["navRects"]:
        assert nav["top"] >= effective_safe_top - 2.5, (
            f"nav {nav['name']} top {nav['top']} < safe_top {effective_safe_top} ({ctx_desc})"
        )
        assert nav["bottom"] <= snap["xtermRect"]["top"] + 1.5, (
            f"nav {nav['name']} bottom {nav['bottom']} "
            f"> terminal top {snap['xtermRect']['top']} ({ctx_desc})"
        )

    # --- Assertion 7: Room rule ---
    root_w = round(snap["viewRect"]["width"])
    bar_h_expected = 55 if root_w >= 560 else 99
    room_px = round(snap["viewRect"]["height"] - snap["paddingBottomPx"])
    should_show_bar = room_px >= bar_h_expected + 108

    if not should_show_bar:
        assert snap["barRect"] is None, (
            f"bar must hide below room floor "
            f"(roomPx={room_px}, floor={bar_h_expected + 108}) ({ctx_desc})"
        )
        assert snap["visibleRows"] >= 4, (
            f"terminal visibleRows={snap['visibleRows']} < 4 when bar yields ({ctx_desc})"
        )
        return snap

    assert snap["barRect"] is not None, (
        f"expected bar visible when roomPx={room_px} >= {bar_h_expected + 108} ({ctx_desc})"
    )
    if nominal_w >= 768:
        assert snap["maxMd"] is False, f"expected maxMd=False at {nominal_w} ({ctx_desc})"

    bar_rect = snap["barRect"]

    # --- Assertion 2: Terminal viewport bottom <= bar top ---
    assert snap["xtermRect"]["bottom"] <= bar_rect["top"] + 1.5, (
        f"terminal viewport bottom {snap['xtermRect']['bottom']} "
        f"> bar top {bar_rect['top']} ({ctx_desc})"
    )

    # --- Assertion 3: Bar bottom <= visible bottom; keyboard down clears safe-bottom ---
    assert bar_rect["bottom"] <= visible_bottom + 1.5, (
        f"bar bottom {bar_rect['bottom']} > visibleBottom {visible_bottom} ({ctx_desc})"
    )
    if kb_mode == "down":
        assert bar_rect["bottom"] <= layout_h - safe["bottom"] + 1.5, (
            f"bar bottom {bar_rect['bottom']} "
            f"> layout_h - safe.bottom ({layout_h - safe['bottom']}) ({ctx_desc})"
        )

    # --- Assertion 4: Bar left and right inside side safe areas ---
    assert len(snap["buttons"]) > 0, f"no buttons in bar ({ctx_desc})"
    for btn in snap["buttons"]:
        assert btn["left"] >= safe["left"] - 1.5, (
            f"button {btn['label']} left {btn['left']} < safe.left {safe['left']} ({ctx_desc})"
        )
        assert btn["right"] <= nominal_w - safe["right"] + 1.5, (
            f"button {btn['label']} right {btn['right']} "
            f"> width - safe.right {nominal_w - safe['right']} ({ctx_desc})"
        )

    # --- Assertion 5: No two key boxes intersect; each >= 44 x 44 px ---
    buttons = snap["buttons"]
    for i, b1 in enumerate(buttons):
        assert b1["width"] >= 43.5 and b1["height"] >= 43.5, (
            f"button {b1['label']} smaller than 44x44: {b1['width']}x{b1['height']} ({ctx_desc})"
        )
        for b2 in buttons[i + 1 :]:
            ix = min(b1["right"], b2["right"]) - max(b1["left"], b2["left"])
            iy = min(b1["bottom"], b2["bottom"]) - max(b1["top"], b2["top"])
            assert not (ix > 0.5 and iy > 0.5), (
                f"buttons {b1['label']} and {b2['label']} intersect by {ix}x{iy} ({ctx_desc})"
            )

    assert snap["scrollY"] == 0, f"scrollY={snap['scrollY']} != 0 ({ctx_desc})"
    return snap


def _assert_scrollback_and_output_stability(
    page: Page,
    before_snap: dict[str, Any],
    surface: str,
    ctx_desc: str,
) -> None:
    """Assertion 6: after scrollback and output, nav boxes stay put and scrollY == 0."""
    if surface == "read-only":
        return
    xterm = before_snap["xtermRect"]
    cx = xterm["left"] + xterm["width"] / 2
    cy = xterm["top"] + xterm["height"] / 2
    page.mouse.move(cx, cy)
    page.mouse.wheel(0, -350)
    if before_snap["barRect"] is not None:
        enter_btn = page.locator('[data-testid="terminal-key-row"] button[aria-label="Enter"]')
        if enter_btn.count() > 0:
            enter_btn.first.dispatch_event(
                "pointerdown", {"pointerId": 1, "clientX": 10, "clientY": 10}
            )
            enter_btn.first.dispatch_event(
                "pointerup", {"pointerId": 1, "clientX": 10, "clientY": 10}
            )
    page.wait_for_timeout(60)
    after = page.evaluate(
        """({ surface }) => {
          const navRects = [];
          if (surface === "host-terminal") {
            const hdr = document.querySelector("main header");
            if (hdr) navRects.push(hdr.getBoundingClientRect().toJSON());
            const tabs = document.querySelector('[role="tablist"][aria-label="Host terminals"]');
            if (tabs) navRects.push(tabs.getBoundingClientRect().toJSON());
          } else if (surface === "terminals-panel") {
            const hdr = document.querySelector('[data-testid="terminals-panel"] > header');
            if (hdr) navRects.push(hdr.getBoundingClientRect().toJSON());
          } else {
            const menuBtn =
              document.querySelector('button[aria-label="Open sidebar"]') ||
              document.querySelector(".chat-header");
            if (menuBtn) navRects.push(menuBtn.getBoundingClientRect().toJSON());
            if (surface === "shell-view") {
              const closeShell = document.querySelector('button[aria-label="Close shell"]');
              const shellBar = closeShell ? closeShell.parentElement : null;
              if (shellBar) navRects.push(shellBar.getBoundingClientRect().toJSON());
            }
          }
          return { scrollY: window.scrollY, navRects };
        }""",
        {"surface": surface},
    )
    assert after["scrollY"] == 0, (
        f"scrollY drifted to {after['scrollY']} after scrollback ({ctx_desc})"
    )
    for before_nav, after_nav in zip(before_snap["navRects"], after["navRects"], strict=False):
        assert abs(before_nav["top"] - after_nav["top"]) <= 1.5, (
            f"nav top drifted {before_nav['top']} -> {after_nav['top']} ({ctx_desc})"
        )
        assert abs(before_nav["bottom"] - after_nav["bottom"]) <= 1.5, (
            f"nav bottom drifted {before_nav['bottom']} -> {after_nav['bottom']} ({ctx_desc})"
        )
    page.mouse.wheel(0, 10_000)


def test_terminal_keys_clearance_matrix(
    browser: Browser,
    terminal_session: tuple[str, str],
) -> None:
    """Full §4.1 overlap, safe-area, keyboard-room, and rotation matrix across all 5 surfaces."""
    base_url, session_id = terminal_session
    _ensure_session_terminals(base_url, session_id)
    seeded_ptys: set[str] = set()
    total_checked = 0

    # 1. Full cross product at 390x844 and 844x390 across Browser, iOS shell, and Android shell.
    for shell in ("browser", "ios", "android"):
        kb_modes = (
            ("down", "ios_bridge_up", "short_strip", "room_floor")
            if shell == "ios"
            else ("down", "overlay_up_0", "overlay_up_40", "layout_resize_up", "short_strip")
            if shell == "browser"
            else ("down", "overlay_up_0", "overlay_up_40", "layout_resize_up")
        )
        context_kwargs: dict[str, Any] = {
            "viewport": {"width": PORTRAIT_MAIN[0], "height": PORTRAIT_MAIN[1]},
            "screen": {"width": PORTRAIT_MAIN[0], "height": PORTRAIT_MAIN[1]},
            "has_touch": True,
            "is_mobile": True,
        }
        record_dir = os.environ.get("OMNIGENT_E2E_RECORD_DIR")
        if record_dir:
            context_kwargs["record_video_dir"] = record_dir

        context = browser.new_context(**context_kwargs)
        state: dict[str, Any] = {"surface": "agent-cli"}
        _install_surface_routes(context, base_url, session_id, state)
        try:
            page = context.new_page()
            page.add_init_script(
                _INIT_SCRIPT_TEMPLATE.replace("__SHELL_KIND__", json.dumps(shell))
            )
            cdp = context.new_cdp_session(page)

            for surface in SURFACES:
                _open_surface(page, base_url, session_id, surface, state, seeded_ptys)
                for nominal_w, nominal_h in (PORTRAIT_MAIN, LANDSCAPE_MAIN):
                    safes = (
                        (SAFE_ZERO, SAFE_PORTRAIT)
                        if nominal_w < nominal_h
                        else (SAFE_ZERO, SAFE_LANDSCAPE)
                    )
                    for safe in safes:
                        _apply_safe_area(page, cdp, shell, surface, safe)
                        for kb_mode in kb_modes:
                            if kb_mode == "room_floor" and nominal_w > nominal_h:
                                continue
                            snap = _measure_and_assert(
                                page,
                                shell=shell,
                                surface=surface,
                                nominal_w=nominal_w,
                                nominal_h=nominal_h,
                                kb_mode=kb_mode,
                                safe=safe,
                            )
                            total_checked += 1
                            if (
                                kb_mode in ("down", "overlay_up_0", "ios_bridge_up")
                                and safe == SAFE_ZERO
                            ):
                                _assert_scrollback_and_output_stability(
                                    page,
                                    snap,
                                    surface,
                                    (
                                        f"shell={shell} surface={surface} "
                                        f"vp={nominal_w}x{nominal_h} kb={kb_mode}"
                                    ),
                                )

                    # Assertion 9: Rotation (portrait keyboard up -> landscape -> portrait)
                    if nominal_w == LANDSCAPE_MAIN[0] and shell in ("browser", "ios"):
                        rot_kb = "ios_bridge_up" if shell == "ios" else "overlay_up_0"
                        _apply_safe_area(page, cdp, shell, surface, SAFE_ZERO)
                        fresh_p = _measure_and_assert(
                            page,
                            shell=shell,
                            surface=surface,
                            nominal_w=390,
                            nominal_h=844,
                            kb_mode=rot_kb,
                            safe=SAFE_ZERO,
                        )
                        fresh_l = _measure_and_assert(
                            page,
                            shell=shell,
                            surface=surface,
                            nominal_w=844,
                            nominal_h=390,
                            kb_mode=rot_kb,
                            safe=SAFE_ZERO,
                        )
                        back_p = _measure_and_assert(
                            page,
                            shell=shell,
                            surface=surface,
                            nominal_w=390,
                            nominal_h=844,
                            kb_mode=rot_kb,
                            safe=SAFE_ZERO,
                        )
                        assert back_p["overlapAttr"] == fresh_p["overlapAttr"]
                        assert (
                            abs(back_p["xtermRect"]["height"] - fresh_p["xtermRect"]["height"])
                            <= 1.5
                        )
                        assert fresh_l["screenW"] == 844 and back_p["screenW"] == 390
                        total_checked += 3

                    # §4.1 mutation sensitivity checks: each of the 5 defects must fail.
                    if (
                        nominal_w == PORTRAIT_MAIN[0]
                        and shell == "browser"
                        and surface == "agent-cli"
                    ):
                        _apply_safe_area(page, cdp, shell, surface, SAFE_ZERO)
                        for mut_name, mut_kb in (
                            ("remove_root_padding", "overlay_up_0"),
                            ("key_row_absolute", "down"),
                            ("clearance_in_scroll", "down"),
                            ("overlap_threshold_80", "short_strip"),
                        ):
                            with pytest.raises(AssertionError):
                                _measure_and_assert(
                                    page,
                                    shell=shell,
                                    surface=surface,
                                    nominal_w=390,
                                    nominal_h=844,
                                    kb_mode=mut_kb,
                                    safe=SAFE_ZERO,
                                    mutation=mut_name,
                                )
                            _open_surface(page, base_url, session_id, surface, state, seeded_ptys)
                    elif (
                        nominal_w == PORTRAIT_MAIN[0]
                        and shell == "ios"
                        and surface == "host-terminal"
                    ):
                        _apply_safe_area(page, cdp, shell, surface, SAFE_ZERO)
                        with pytest.raises(AssertionError):
                            _measure_and_assert(
                                page,
                                shell=shell,
                                surface=surface,
                                nominal_w=390,
                                nominal_h=844,
                                kb_mode="ios_bridge_up",
                                safe=SAFE_ZERO,
                                mutation="ios_host_overlap_zero",
                            )
                        _open_surface(page, base_url, session_id, surface, state, seeded_ptys)
        finally:
            context.close()

    # 2. Other four viewports (360x640, 430x932, 667x375, 932x430): browser only.
    context = browser.new_context(
        viewport={"width": 360, "height": 640},
        screen={"width": 360, "height": 640},
        has_touch=True,
        is_mobile=True,
    )
    state = {"surface": "agent-cli"}
    _install_surface_routes(context, base_url, session_id, state)
    try:
        page = context.new_page()
        page.add_init_script(_INIT_SCRIPT_TEMPLATE.replace("__SHELL_KIND__", '"browser"'))
        cdp = context.new_cdp_session(page)
        for surface in SURFACES:
            _open_surface(page, base_url, session_id, surface, state, seeded_ptys)
            for nominal_w, nominal_h in OTHER_VIEWPORTS:
                safes = (
                    (SAFE_ZERO, SAFE_PORTRAIT)
                    if nominal_w < nominal_h
                    else (SAFE_ZERO, SAFE_LANDSCAPE)
                )
                for safe in safes:
                    _apply_safe_area(page, cdp, "browser", surface, safe)
                    for kb_mode in ("down", "overlay_up_0"):
                        _measure_and_assert(
                            page,
                            shell="browser",
                            surface=surface,
                            nominal_w=nominal_w,
                            nominal_h=nominal_h,
                            kb_mode=kb_mode,
                            safe=safe,
                        )
                        total_checked += 1
    finally:
        context.close()

    print(f"[clearance-matrix] verified {total_checked} combinations across 5 surfaces")
    assert total_checked >= 250
