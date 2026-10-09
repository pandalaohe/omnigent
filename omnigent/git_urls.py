"""Git remote URL helpers shared by the host daemon and the server.

A git remote URL can carry a credential in its userinfo (``https://user:token@…``)
or its query (``…?private_token=…``). Everything Omnigent shows or hands an
agent must be credential-free, so both sides redact with the same function.
"""

from __future__ import annotations


def redact_remote_url(url: str) -> str:
    """Strip userinfo, query and fragment from a git remote URL.

    For a URL with ``://``, everything after the scheme up to and including
    the last ``@`` is dropped, then the query and fragment are stripped. A
    path ``@`` cannot be told apart from malformed userinfo, so it is treated
    as userinfo on purpose: credentials win over a rare path character. The
    scp-like form (``user@host:path``) is returned unchanged: its ``user@`` is
    an ssh login, not a secret, and rewriting it would change the remote.

    :param url: Remote URL as git stores it, e.g.
        ``"https://user:token@h/x.git?private_token=t#f"`` or
        ``"git@h:org/repo.git"``.
    :returns: The URL without credentials, e.g. ``"https://h/x.git"``.
    """
    if "://" not in url:
        return url
    scheme, rest = url.split("://", 1)
    # Fail closed: find the last '@' before stripping delimiters, since a
    # password may hold a raw '?' or '#'.
    if "@" in rest:
        rest = rest.rsplit("@", 1)[-1]
    rest = rest.split("?", 1)[0].split("#", 1)[0]
    return f"{scheme}://{rest}"
