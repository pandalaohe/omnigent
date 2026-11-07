"""The example passes budget fields, never the credential-bearing response."""

import json
import runpy
import sys
import urllib.error
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/examples/report_litellm_usage.py"


@pytest.mark.parametrize("check_only", [False, True])
def test_litellm_example_excludes_credentials(monkeypatch, capsys, check_only):
    module = runpy.run_path(str(SCRIPT))
    main = module["main"]
    calls = []

    def request(url, key, body=None):
        calls.append((url, key, body))
        if body is None:
            return json.dumps(
                {
                    "key": "private-marker",
                    "info": {
                        "token": "private-marker",
                        "config": {"provider": "private-marker"},
                        "spend": 12,
                        "max_budget": None,
                    },
                }
            ).encode()
        return b'{"applied":0,"held":[],"skipped":[],"older_sections":[]}'

    main.__globals__["request"] = request
    for key, value in {
        "LITELLM_URL": "http://vendor.test",
        "LITELLM_API_KEY": "vendor-test",
        "OMNIGENT_URL": "http://usage.test",
        "USAGE_KEY": "usage-test",
        "USAGE_ACCOUNT": "personal",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), *(["--check-only"] if check_only else [])])
    assert main() == 0
    output = capsys.readouterr()
    assert "private-marker" not in output.out + output.err
    assert len(calls) == (1 if check_only else 2)
    if not check_only:
        assert calls[1][1] == "usage-test"
        assert json.loads(calls[1][2]) == {"info": {"spend": 12, "max_budget": None}}


def test_litellm_example_withholds_http_error(monkeypatch, capsys):
    main = runpy.run_path(str(SCRIPT))["main"]

    def refused(*_args):
        raise urllib.error.HTTPError("http://vendor.test", 403, "private-marker", None, None)

    main.__globals__["request"] = refused
    monkeypatch.setenv("LITELLM_URL", "http://vendor.test")
    monkeypatch.setenv("LITELLM_API_KEY", "vendor-test")
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--check-only"])
    assert main() == 1
    output = capsys.readouterr()
    assert "403" in output.err
    assert "private-marker" not in output.out + output.err
