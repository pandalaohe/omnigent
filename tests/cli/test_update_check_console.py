"""Git console output is captured as bytes even with localized diagnostics."""

import subprocess
from pathlib import Path

import pytest

from omnigent import update_check as updates
from omnigent.util import console_output


@pytest.mark.parametrize("site", ["fetch-count", "head", "local-count", "nightly", "remote"])
def test_git_console_reads_decode_bytes(monkeypatch, site):
    monkeypatch.setattr(console_output, "_windows_code_pages", lambda: (936,))

    def git(argv, **kwargs):
        assert not kwargs.get("text")
        if "rev-list" in argv:
            output = b"3\n"
        elif "rev-parse" in argv:
            output = "就绪\n".encode("gbk")
        elif "--tags" in argv:
            output = "hash\trefs/tags/v0.8.0.dev20260804\n#就绪\n".encode("gbk")
        else:
            output = ("b" * 40 + "\trefs/heads/就绪\n").encode("gbk")
        return subprocess.CompletedProcess(argv, 0, output, "就绪".encode("gbk"))

    monkeypatch.setattr(updates, "run_git", git)
    root = Path("/opt/work/omnigent/fork/trial")
    if site == "fetch-count":
        assert updates._fetch_and_count(root, "main") == 3
    elif site == "head":
        assert updates._get_head_sha(root) == "就绪"
    elif site == "local-count":
        assert updates._local_rev_list_count(root) == 3
    elif site == "nightly":
        assert updates._latest_nightly_version() == "0.8.0.dev20260804"
    else:
        assert updates._remote_git_head("git+https://example.test/repo.git@就绪") == "b" * 40
