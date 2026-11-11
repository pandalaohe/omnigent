"""Host diagnostic tails preserve localized console output."""

import io

import pytest

from omnigent.host import connect, local_server, post_bind_hook
from omnigent.util import console_output


@pytest.mark.parametrize("reader", ["runner", "server", "post-bind"])
@pytest.mark.parametrize(
    "output,expected", [("命令失败".encode("gbk"), "命令失败"), (b"bad\x81", "bad�")]
)
def test_localized_host_console_tails(monkeypatch, tmp_path, reader, output, expected):
    monkeypatch.setattr(console_output, "_windows_code_pages", lambda: (936, 1252))
    log = tmp_path / "console.log"
    log.write_bytes(output)
    if reader == "runner":
        text = connect._read_log_tail(log)
    elif reader == "server":
        text = local_server._read_log_tail(log)
    else:
        text = post_bind_hook._read_tail(io.BytesIO(output))
    assert text == expected
