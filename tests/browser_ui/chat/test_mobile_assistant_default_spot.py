from playwright.sync_api import Page, expect

from tests.browser_ui.chat.session_contract import ChatSessionContract


def test_phone_chat_has_no_floating_assistant(
    page: Page, chat_session_contract: ChatSessionContract
) -> None:
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(chat_session_contract.url)
    expect(page.get_by_role("button", name="Conversation actions", exact=True)).to_be_visible()
    expect(page.get_by_test_id("mobile-floating-assistant")).to_have_count(0)
