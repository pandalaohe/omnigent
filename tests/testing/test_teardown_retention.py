"""Teardown retention when the OS denies the process-table read.

The observed failure (26BUG-164) is an EPERM from ``sysctl(KERN_PROC)`` during
``pytest_unconfigure``. These regressions pin the two properties the fix must
keep: nothing is signalled once visibility is denied, and the teardown retains
the paths it can no longer prove are unused instead of deleting them.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock

import psutil
import pytest

from omnigent.testing.process_reaper import (
    OWNER_MARKER_NAME,
    ProcessScanUnavailable,
    reap_leaked_omnigent_processes,
)

_DENIALS = [PermissionError(1, "Operation not permitted"), psutil.AccessDenied(0, "sysctl denied")]


@pytest.fixture()
def data_dir(tmp_path: Path) -> Path:
    """A stand-in for the session's throwaway ``OMNIGENT_DATA_DIR``."""
    path = tmp_path / "omnigent-pytest-fixture"
    path.mkdir()
    return path


def _owned_proc(data_dir: Path) -> Mock:
    """A fake process the matcher would attribute to *data_dir*."""
    proc = Mock(spec=psutil.Process, pid=424242)
    proc.cmdline.return_value = [
        "omnigent",
        "runner",
        f"--database-uri=sqlite:///{data_dir}/chat.db",
    ]
    proc.environ.return_value = {"OMNIGENT_DATA_DIR": str(data_dir)}
    return proc


def _assert_no_signals(proc: Mock) -> None:
    proc.terminate.assert_not_called()
    proc.kill.assert_not_called()
    proc.wait.assert_not_called()


@pytest.mark.parametrize("cause", _DENIALS)
def test_denied_at_call_time_is_typed_without_signalling(
    monkeypatch: pytest.MonkeyPatch, data_dir: Path, cause: Exception
) -> None:
    """``psutil.process_iter()`` itself raising must surface, not read as clean."""

    def denied(*_args: object, **_kwargs: object) -> object:
        raise cause

    monkeypatch.setattr(psutil, "process_iter", denied)
    with pytest.raises(ProcessScanUnavailable) as failure:
        reap_leaked_omnigent_processes(data_dir)
    assert "process table enumeration denied" in str(failure.value)
    assert failure.value.__cause__ is cause


@pytest.mark.parametrize("cause", _DENIALS)
def test_denied_mid_iteration_after_one_owned_process_signals_nothing(
    monkeypatch: pytest.MonkeyPatch, data_dir: Path, cause: Exception
) -> None:
    """Attribution succeeding for one process must not signal it if the scan dies."""

    proc = _owned_proc(data_dir)

    def half_visible():
        yield proc
        raise cause

    monkeypatch.setattr(psutil, "process_iter", half_visible)
    with pytest.raises(ProcessScanUnavailable) as failure:
        reap_leaked_omnigent_processes(data_dir)
    assert "process table enumeration denied" in str(failure.value)
    assert failure.value.__cause__ is cause
    _assert_no_signals(proc)


def _hook_dirs(root: Path) -> tuple[Path, Path, Path]:
    data_dir = root / "omnigent-pytest-hook"
    harness = root / "omni-h-hook"
    browser = root / "browser-guard"
    for path in (data_dir, harness, browser):
        path.mkdir()
    (data_dir / OWNER_MARKER_NAME).write_text(
        json.dumps({"pid": 1, "create_time": 1.0}), encoding="utf-8"
    )
    return data_dir, harness, browser


def test_unconfigure_retains_evidence_and_still_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A denied reap must keep every path, say UNVERIFIED, and raise."""
    conftest = pytest.importorskip("tests.conftest")
    data_dir, harness, browser = _hook_dirs(tmp_path)
    monkeypatch.setattr(conftest, "_TEST_OMNIGENT_DATA_DIR", data_dir)
    monkeypatch.setattr(conftest, "_TEST_HARNESS_TMP_PARENT", harness)
    monkeypatch.setattr(conftest, "_BROWSER_GUARD_DIR", str(browser))

    def denied(_data_dir: object) -> tuple[list[str], list[str]]:
        raise ProcessScanUnavailable("process table enumeration denied: fixture")

    monkeypatch.setattr("omnigent.testing.process_reaper.reap_leaked_omnigent_processes", denied)
    with pytest.raises(ProcessScanUnavailable):
        conftest.pytest_unconfigure(None)

    assert data_dir.is_dir()
    assert (data_dir / OWNER_MARKER_NAME).is_file()
    assert harness.is_dir()
    assert browser.is_dir()
    stderr = capsys.readouterr().err
    assert "leak state UNVERIFIED" in stderr
    for path in (data_dir, harness, browser):
        assert f"retained: {path}" in stderr
    marker = json.loads((tmp_path / "omnigent-teardown-retention.json").read_text())
    assert marker["status"] == "unverified"
    assert sorted(marker["retained"]) == sorted(str(p) for p in (data_dir, harness, browser))


def test_failed_retention_request_never_reads_as_a_safe_delete(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A marker write failure must be reported, not turned into silent deletion.

    The wrapper keeps the run dir without a marker only because this run failed
    and its data dir is still present; the hook has to say that out loud.
    """
    conftest = pytest.importorskip("tests.conftest")
    data_dir, harness, browser = _hook_dirs(tmp_path)
    monkeypatch.setattr(conftest, "_TEST_OMNIGENT_DATA_DIR", data_dir)
    monkeypatch.setattr(conftest, "_TEST_HARNESS_TMP_PARENT", harness)
    monkeypatch.setattr(conftest, "_BROWSER_GUARD_DIR", str(browser))
    # A marker parent that does not exist makes the real helper's write fail.
    monkeypatch.setattr(conftest, "_TEARDOWN_RETENTION_FILE", "missing-dir/retention.json")

    def denied(_data_dir: object) -> tuple[list[str], list[str]]:
        raise ProcessScanUnavailable("process table enumeration denied: fixture")

    monkeypatch.setattr("omnigent.testing.process_reaper.reap_leaked_omnigent_processes", denied)
    with pytest.raises(ProcessScanUnavailable):
        conftest.pytest_unconfigure(None)

    assert data_dir.is_dir()
    assert (data_dir / OWNER_MARKER_NAME).is_file()
    assert harness.is_dir()
    assert browser.is_dir()
    stderr = capsys.readouterr().err
    assert "run-tmp retention request FAILED" in stderr
    assert "leak state UNVERIFIED" in stderr
    for path in (data_dir, harness, browser):
        assert f"retained: {path}" in stderr
    assert not (tmp_path / "missing-dir").exists()


def test_unconfigure_keeps_deleting_after_a_clean_reap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The successful path is unchanged: reap, report, delete, no marker."""
    conftest = pytest.importorskip("tests.conftest")
    data_dir, harness, browser = _hook_dirs(tmp_path)
    monkeypatch.setattr(conftest, "_TEST_OMNIGENT_DATA_DIR", data_dir)
    monkeypatch.setattr(conftest, "_TEST_HARNESS_TMP_PARENT", harness)
    monkeypatch.setattr(conftest, "_BROWSER_GUARD_DIR", str(browser))
    monkeypatch.setattr(
        "omnigent.testing.process_reaper.reap_leaked_omnigent_processes",
        lambda _data_dir: ([], []),
    )

    conftest.pytest_unconfigure(None)

    assert not data_dir.exists()
    assert not harness.exists()
    assert not browser.exists()
    assert not (tmp_path / "omnigent-teardown-retention.json").exists()
