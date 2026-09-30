"""Tests for the framework-owned session-handover tool."""

from omnigent.tools.builtins.session_handover import SysSessionHandoverTool


def test_session_handover_schema_is_self_scoped() -> None:
    schema = SysSessionHandoverTool().get_schema()["function"]

    assert schema["name"] == "sys_session_handover"
    assert schema["parameters"]["required"] == ["handover"]
    assert set(schema["parameters"]["properties"]) == {"handover", "rotate"}
    assert schema["parameters"]["properties"]["handover"]["minLength"] == 1
    assert schema["parameters"]["properties"]["rotate"]["default"] is True
    assert "cleared" in schema["description"]
    assert "live sub-agents" in schema["description"]
