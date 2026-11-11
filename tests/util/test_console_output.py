"""Console decoding preserves localized output and tolerates corrupt bytes."""

from types import SimpleNamespace

import pytest

from omnigent.util import console_output


@pytest.mark.parametrize(
    "output,pages,expected",
    [
        ("就绪".encode(), (936, 1252), "就绪"),
        ("就绪".encode("gbk"), (936, 1252), "就绪"),
        ("就绪".encode("gbk"), (65001, 936, 1252), "就绪"),
        (b"caf\xe9", (65001, 936, 1252), "café"),
        (b"bad\x81", (65001, 936, 1252), "bad�"),
        (b"bad\x81", (0, 999999), "bad�"),
        (b"", (), ""),
        ("already decoded", (), "already decoded"),
    ],
)
def test_decode_console_output(monkeypatch, output, pages, expected):
    monkeypatch.setattr(console_output, "_windows_code_pages", lambda: pages)
    assert console_output.decode_console_output(output) == expected


def test_windows_code_pages_use_console_oem_then_ansi(monkeypatch):
    monkeypatch.setattr(console_output, "IS_WINDOWS", True)
    monkeypatch.setattr(
        console_output.ctypes,
        "WinDLL",
        lambda *_args, **_kwargs: SimpleNamespace(
            GetConsoleOutputCP=lambda: 0, GetOEMCP=lambda: 936, GetACP=lambda: 1252
        ),
        raising=False,
    )
    assert console_output._windows_code_pages() == (0, 936, 1252)
    assert console_output.decode_console_output("就绪".encode("gbk")) == "就绪"


def test_code_page_api_failure_still_returns_text(monkeypatch):
    monkeypatch.setattr(console_output, "IS_WINDOWS", True)

    def unavailable(*_args, **_kwargs):
        raise OSError("API unavailable")

    monkeypatch.setattr(console_output.ctypes, "WinDLL", unavailable, raising=False)
    assert console_output.decode_console_output(b"bad\x81") == "bad�"
