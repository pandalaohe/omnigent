"""Phone composer geometry with the advertised Fast control present."""

import pytest
from playwright.sync_api import Page, Request, expect

from tests.browser_ui.chat._geometry_helpers import (
    assert_no_overlap,
    assert_same_vertical_center,
    box,
    open_landing,
    open_live,
    right_inset,
)
from tests.browser_ui.chat.session_contract import ChatSessionContract, model_option


@pytest.mark.parametrize("surface", ["landing", "live"])
@pytest.mark.parametrize("menu", [False, True], ids=["toggle", "menu"])
def test_fast_control_keeps_send_and_stop_inside_phone_composer(
    page: Page, chat_session_contract: ChatSessionContract, surface: str, menu: bool
) -> None:
    chat = chat_session_contract
    model = model_option(
        "gpt-long-model",
        display_name="GPT Long Model (maximum context)",
        is_default=True,
    )
    model["serviceTiers"] = [{"id": "priority", "name": "Fast"}]
    if menu:
        model["serviceTiers"].append({"id": "ultrafast", "name": "Ultrafast"})
    chat.set_catalog(harness="codex-native", models=[model], selected_model="gpt-long-model")
    chat.update_session(
        labels={"omnigent.wrapper": "codex-native-ui", "omnigent.speed_tier": "fast"},
        approval_mode="full-access",
        reasoning_effort="high",
    )
    viewport = {"width": 390, "height": 844}
    if surface == "landing":
        chat.contract.json(
            "/v1/calling-defaults/resolve",
            {
                "agent_id": chat.agent_id,
                "harness": "codex-native",
                "model": "gpt-long-model",
                "effort": "high",
                "speed": "standard",
                "permission": "full-access",
                "sources": {},
                "problems": [],
            },
        )
        open_landing(page, chat, viewport)
        input_field = page.get_by_test_id("new-chat-landing-input")
        model_label = page.get_by_test_id("new-chat-landing-agent-config-value")
        permission = page.get_by_test_id("new-chat-landing-permission-chip")
        submit = page.get_by_test_id("new-chat-landing-submit")
    else:
        chat.contract.json(f"/v1/sessions/{chat.session_id}/codex_goal", {"goal": None})

        def patch_speed(request: Request) -> dict:
            speed = request.post_data_json["speed_tier"]
            assert speed in {"standard", "fast", "ultrafast"}
            chat.update_session(
                labels={"omnigent.wrapper": "codex-native-ui", "omnigent.speed_tier": speed}
            )
            return chat._session()

        chat.contract.json(f"/v1/sessions/{chat.session_id}", patch_speed, method="PATCH")
        open_live(page, chat, viewport)
        input_field = page.get_by_label("Message the agent")
        model_label = page.get_by_test_id("composer-agent-config-value")
        permission = page.get_by_test_id("composer-permission-chip")
        submit = page.get_by_role("button", name="Send", exact=True)

    input_field.fill("Check compact phone controls")
    fast = page.get_by_test_id(
        "new-chat-speed-trigger" if surface == "landing" else "composer-speed-trigger"
    )
    expect(fast).to_be_visible()
    assert fast.inner_text() == ""
    for width in [375, 390, 430]:
        page.set_viewport_size({"width": width, "height": 844})
        expect(model_label).to_be_hidden()
        expect(permission).to_be_visible()
        expect(permission.locator("span")).to_be_hidden()
        card = box(page.locator("[data-composer-card]"))
        assert right_inset(card, box(submit)) >= 11
        assert_same_vertical_center(box(fast), box(submit))
        assert_no_overlap(box(fast), box(submit))
        assert page.evaluate("document.documentElement.scrollWidth") <= width

    page.set_viewport_size(viewport)
    if menu:
        fast.click()
        expect(
            page.get_by_role(
                "menuitemradio", name="Standard" if surface == "landing" else "Fast", exact=True
            )
        ).to_have_attribute("aria-checked", "true")
        page.get_by_role("menuitemradio", name="Ultrafast", exact=True).click()
        expect(fast).to_have_attribute("aria-label", "Speed: Ultrafast")
        assert fast.locator("svg.lucide-zap").count() == 1
        speed_menu = page.get_by_test_id(
            "new-chat-speed-menu" if surface == "landing" else "composer-speed-menu"
        )
        expect(speed_menu).to_have_count(0)
        fast.click()
        expect(page.get_by_role("menuitemradio", name="Ultrafast", exact=True)).to_have_attribute(
            "aria-checked", "true"
        )
        page.get_by_role("menuitemradio", name="Standard", exact=True).click()
        expect(fast).to_have_attribute("aria-label", "Speed: Standard")
        assert fast.locator("svg.lucide-zap-off").count() == 1
        expect(speed_menu).to_have_count(0)
    elif surface == "landing":
        fast.click()
        expect(fast).to_have_attribute("aria-pressed", "true")
        fast.press("Space")
        expect(fast).to_have_attribute("aria-pressed", "false")
    if surface == "live":
        if not menu:
            expect(fast).to_have_attribute("aria-pressed", "true")
        input_field.fill("")
        chat.emit_busy()
        stop = page.get_by_role("button", name="Interrupt", exact=True)
        expect(stop).to_be_visible()
        for width in [375, 390, 430]:
            page.set_viewport_size({"width": width, "height": 844})
            card = box(page.locator("[data-composer-card]"))
            assert right_inset(card, box(stop)) >= 11
            assert_same_vertical_center(box(fast), box(stop))
            assert_no_overlap(box(fast), box(stop))
        page.set_viewport_size(viewport)
