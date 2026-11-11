"""Built settings UI: create, copy, close, regenerate and delete personal keys."""

import pytest
from playwright.sync_api import Page, expect

from tests.browser_ui.conftest import BrowserContract
from tests.browser_ui.test_sessions_appearance import appearance_features as appearance_features
from tests.browser_ui.test_sessions_appearance import appearance_url as appearance_url


@pytest.mark.parametrize("width", [1440, 390])
def test_api_key_journey(
    page: Page, appearance_url: str, browser_contract: BrowserContract, width: int
):
    page.set_viewport_size({"width": width, "height": 900})
    browser_contract.json(
        "/v1/info",
        {
            "single_user": False,
            "accounts_enabled": True,
            "login_url": "/login",
            "api_keys_available": True,
            "features": {},
        },
    )
    rows = []
    value = "synthetic-browser-key"

    def listing(_):
        return {"keys": rows, "read_only": [], "available_scopes": ["usage", "tasks"]}

    def create(request):
        assert request.post_data_json["scopes"] == ["*"]
        key = {
            "id": "example",
            "name": "feed",
            "prefix": "omk_display",
            "scopes": ["*"],
            "created_at": 1,
            "expires_at": None,
            "last_used_at": None,
            "regenerated_at": None,
        }
        rows.append(key)
        return {"key": key, "value": value}

    def regenerate(_):
        rows[0]["prefix"] = "omk_rotated"
        return {"key": rows[0], "value": value + "-rotated"}

    def delete(_):
        rows.clear()
        return {}

    browser_contract.json("/v1/me/api-keys", listing)
    browser_contract.json("/v1/me/api-keys", create, method="POST", status=201)
    browser_contract.json("/v1/me/api-keys/example/regenerate", regenerate, method="POST")
    browser_contract.json("/v1/me/api-keys/example", delete, method="DELETE")
    page.add_init_script("navigator.clipboard.writeText = async () => {}")
    page.goto(appearance_url + "/settings/api-keys")
    page.get_by_role("button", name="New API key").click()
    page.get_by_label("Name", exact=True).fill("feed")
    page.get_by_role("button", name="Create API key", exact=True).click()
    expect(page.get_by_text(value, exact=True)).to_be_visible()
    page.get_by_role("button", name="Copy", exact=True).click()
    page.get_by_role("button", name="Done", exact=True).click()
    expect(page.get_by_text(value, exact=True)).to_have_count(0)
    page.get_by_role("button", name="Regenerate", exact=True).click()
    page.get_by_role("button", name="Regenerate API key", exact=True).click()
    expect(page.get_by_text(value + "-rotated", exact=True)).to_be_visible()
    page.keyboard.press("Escape")
    expect(page.get_by_text(value + "-rotated", exact=True)).to_have_count(0)
    page.get_by_role("button", name="Delete", exact=True).click()
    page.get_by_role("button", name="Delete API key", exact=True).click()
    expect(page.get_by_text("No API keys yet.")).to_be_visible()
