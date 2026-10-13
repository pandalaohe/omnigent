"""Tests for the Codex elicitation protocol adapters.

These are pure-function tests — no HTTP or runtime needed.
"""

from __future__ import annotations

import pytest

from omnigent.errors import OmnigentError
from omnigent.runtime.subagent_block_notifier import _block_reason
from omnigent.server.routes._codex_elicitation import (
    _codex_command_preview,
    _codex_mcp_elicitation_response,
    _codex_mcp_persist_modes,
    _execpolicy_amendment,
    _json_preview,
    _string_list_answer,
    parse_codex_elicitation_request,
)
from omnigent.server.schemas import ElicitationResult

# ── parse_codex_elicitation_request ──────────────────────────────────


class TestParseCodexElicitationRequest:
    """Tests for the top-level request parser."""

    def test_missing_method_raises(self) -> None:
        with pytest.raises(OmnigentError, match="non-empty method"):
            parse_codex_elicitation_request({"id": 1, "params": {}})

    def test_empty_method_raises(self) -> None:
        with pytest.raises(OmnigentError, match="non-empty method"):
            parse_codex_elicitation_request({"id": 1, "method": "", "params": {}})

    def test_non_dict_params_raises(self) -> None:
        with pytest.raises(OmnigentError, match="params must be an object"):
            parse_codex_elicitation_request(
                {"id": 1, "method": "mcpServer/elicitation/request", "params": "bad"}
            )

    def test_missing_id_raises(self) -> None:
        with pytest.raises(OmnigentError, match="string or integer id"):
            parse_codex_elicitation_request(
                {"method": "mcpServer/elicitation/request", "params": {}}
            )

    def test_unsupported_method_raises(self) -> None:
        with pytest.raises(OmnigentError, match="Unsupported"):
            parse_codex_elicitation_request({"id": 1, "method": "unknown/method", "params": {}})

    def test_valid_mcp_form_request(self) -> None:
        req = parse_codex_elicitation_request(
            {
                "id": 1,
                "method": "mcpServer/elicitation/request",
                "params": {
                    "mode": "form",
                    "message": "Need input",
                    "requestedSchema": {"type": "object"},
                },
            }
        )
        assert req.method == "mcpServer/elicitation/request"
        assert req.params.mode == "form"

    def test_valid_command_approval(self) -> None:
        req = parse_codex_elicitation_request(
            {
                "id": 2,
                "method": "item/commandExecution/requestApproval",
                "params": {"command": "npm test"},
            }
        )
        assert req.method == "item/commandExecution/requestApproval"

    def test_mcp_notice_elides_image_and_preserves_approval_detail(self) -> None:
        meta = {
            "codex_approval_kind": "mcp_tool_call",
            "tool_name": "img2img",
            "tool_description": "Full description " * 100,
            "tool_params": {
                "ref_image": "iVBORw0KGgo" + "A" * 10000,
                "prompt": "Change the tie to green",
                "image": "data:image/png;base64,AAAA",
                "options": {"size": "large"},
            },
        }
        req = parse_codex_elicitation_request(
            {
                "id": 1,
                "method": "mcpServer/elicitation/request",
                "params": {
                    "serverName": "image-provider",
                    "mode": "form",
                    "message": "Approve img2img",
                    "requestedSchema": {"type": "object"},
                    "_meta": meta,
                },
            }
        )
        preview = _block_reason({"params": req.params.model_dump()})
        assert preview is not None
        assert "image-provider.img2img" in preview
        assert "ref_image=<omitted>" in preview
        assert "Change the tie to green" in preview
        assert "iVBOR" not in preview
        assert "data:image" not in preview
        assert "Full description" not in preview
        assert len(preview) <= 200
        assert req.params.model_extra is not None
        assert req.params.model_extra["_meta"] == meta
        assert req.params.message == "Approve img2img"
        assert req.params.content_preview == _json_preview(req.codex_params)


# ── Codex MCP approval persistence ───────────────────────────────────


class TestCodexMcpApprovalPersistence:
    """Tests for Codex's session and durable MCP approval choices."""

    @pytest.mark.parametrize("mode", ["session", "always"])
    def test_returns_advertised_persistence_mode(self, mode: str) -> None:
        result = ElicitationResult.model_validate({"action": "accept", "_meta": {"persist": mode}})

        response = _codex_mcp_elicitation_response(
            result,
            "mcpServer/elicitation/request",
            {
                "_meta": {
                    "codex_approval_kind": "mcp_tool_call",
                    "persist": ["session", "always"],
                }
            },
        )

        assert response == {
            "action": "accept",
            "content": None,
            "_meta": {"persist": mode},
        }

    def test_rejects_unadvertised_persistence_mode(self) -> None:
        result = ElicitationResult.model_validate(
            {"action": "accept", "_meta": {"persist": "always"}}
        )

        with pytest.raises(OmnigentError, match="was not advertised"):
            _codex_mcp_elicitation_response(
                result,
                "mcpServer/elicitation/request",
                {
                    "_meta": {
                        "codex_approval_kind": "mcp_tool_call",
                        "persist": ["session"],
                    }
                },
            )

    def test_ignores_malformed_advertised_modes(self) -> None:
        params = {
            "_meta": {
                "codex_approval_kind": "mcp_tool_call",
                "persist": ["session", {"unexpected": "object"}, None],
            }
        }

        assert _codex_mcp_persist_modes(params) == {"session"}


# ── _string_list_answer ──────────────────────────────────────────────


class TestStringListAnswer:
    """Tests for answer normalization."""

    def test_string_input(self) -> None:
        assert _string_list_answer("React") == ["React"]

    def test_empty_string(self) -> None:
        assert _string_list_answer("") == []

    def test_list_input(self) -> None:
        assert _string_list_answer(["a", "b"]) == ["a", "b"]

    def test_list_with_non_strings(self) -> None:
        assert _string_list_answer(["a", 123, "b"]) == ["a", "b"]

    def test_none_input(self) -> None:
        assert _string_list_answer(None) == []

    def test_numeric_input(self) -> None:
        assert _string_list_answer(42) == ["42"]


# ── _codex_command_preview ───────────────────────────────────────────


class TestCodexCommandPreview:
    """Tests for command preview extraction."""

    def test_string_command(self) -> None:
        assert _codex_command_preview({"command": "npm test"}) == "npm test"

    def test_list_command(self) -> None:
        assert _codex_command_preview({"command": ["npm", "test"]}) == "npm test"

    def test_empty_command(self) -> None:
        assert _codex_command_preview({"command": ""}) is None

    def test_missing_command(self) -> None:
        assert _codex_command_preview({}) is None


# ── _json_preview ────────────────────────────────────────────────────


class TestJsonPreview:
    """Tests for the bounded preview function."""

    def test_simple_object(self) -> None:
        result = _json_preview({"key": "value"})
        assert '"key"' in result

    def test_truncated(self) -> None:
        big = {"k": "x" * 2000}
        result = _json_preview(big)
        assert len(result) <= 1024


# ── _execpolicy_amendment ────────────────────────────────────────────


class TestExecpolicyAmendment:
    """Tests for execpolicy amendment validation."""

    def test_none_returns_none(self) -> None:
        assert _execpolicy_amendment(None) is None

    def test_valid_list(self) -> None:
        assert _execpolicy_amendment(["pytest", "-v"]) == ["pytest", "-v"]

    def test_empty_list_raises(self) -> None:
        with pytest.raises(OmnigentError, match="non-empty list"):
            _execpolicy_amendment([])

    def test_non_list_raises(self) -> None:
        with pytest.raises(OmnigentError, match="non-empty list"):
            _execpolicy_amendment("pytest")

    def test_list_with_non_strings_raises(self) -> None:
        with pytest.raises(OmnigentError, match="non-empty list"):
            _execpolicy_amendment(["pytest", 42])
