"""Tests for the shared git remote URL redaction."""

from __future__ import annotations

import pytest

from omnigent.git_urls import redact_remote_url


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        # Credential and token-bearing URLs lose userinfo, query and fragment.
        (
            "https://user:token@h/x.git?private_token=t#f",
            "https://h/x.git",
        ),
        ("ssh://git@h/x", "ssh://h/x"),
        ("http://user@h", "http://h"),
        ("ssh://user:pass@h:2222/x?y=z#f", "ssh://h:2222/x"),
        ("https://user@[::1]:8080/x", "https://[::1]:8080/x"),
        # A credential-free URL is already redacted.
        ("https://h/x.git", "https://h/x.git"),
        # An '@' in a path is treated as userinfo on purpose: credentials win
        # over a rare path character.
        ("https://h/x@y", "https://y"),
        # A malformed authority must not leak: the last '@' wins, and a raw
        # '/', '?' or '#' in userinfo cannot strand it.
        ("https://demo:EXAMPLE?marker@host/repo", "https://host/repo"),
        ("https://demo:EXAMPLE#marker@host/repo", "https://host/repo"),
        ("https://u:pa/ss@h/x", "https://h/x"),
        ("https://u:EXAMPLE/part?tail@h/x", "https://h/x"),
        ("https://u:EXAMPLE/part#tail@h/x", "https://h/x"),
        ("https://EXAMPLE/part@h/x", "https://h/x"),
        ("https://u/EXAMPLE:part@h/x", "https://h/x"),
        # scp-like form: the user is an ssh login, not a secret; keep it whole.
        ("git@github.com:org/repo.git", "git@github.com:org/repo.git"),
        ("user@host:path", "user@host:path"),
        # Local paths have nothing to strip.
        ("/opt/work/omnigent/fork/myrepo", "/opt/work/omnigent/fork/myrepo"),
    ],
)
def test_redact_remote_url(url: str, expected: str) -> None:
    assert redact_remote_url(url) == expected
