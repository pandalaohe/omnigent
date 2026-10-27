"""The host load CLI reads only its local file and always emits schema 1."""

import json
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from omnigent.cli import cli
from omnigent.host.load_level import utc_iso
from omnigent.util.atomic_json import atomic_write_json


@pytest.mark.parametrize(
    "state",
    [
        "fresh",
        "transient-failure",
        "missing",
        "malformed",
        "warmup",
        "stale",
        "unknown",
        "bad-time",
        "bad-cores",
        "future",
        "bad-lines",
        "bad-level",
    ],
)
def test_host_load_contract(state: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path))
    now = time.time()
    path = tmp_path / "run" / "host-load.json"
    payload = {
        "schema": 1,
        "host_id": "host_a",
        "sampled_at": utc_iso(now),
        "reading_at": utc_iso(now),
        "cpu_count": 24,
        "load1": 96.0,
        "load_equiv": 96.0,
        "estimate": False,
        "load_per_core": 4.0,
        "mem_pct": 50.0,
        "level": "heavy_hold",
        "reason": "load",
        "lines": {"heavy": 4.0, "new_session": 5.0},
        "lines_synced_at": None,
        "path": str(path),
    }
    if state == "warmup":
        payload.update(
            reading_at=None, load1=None, load_equiv=None, load_per_core=None, level="unknown"
        )
    elif state == "transient-failure":
        payload.update(cpu_count=None, load1=None, load_equiv=None, load_per_core=None)
        payload["reading_at"] = utc_iso(now - 60)
    elif state == "stale":
        payload["reading_at"] = utc_iso(now - 200)
    elif state == "unknown":
        payload["level"] = "unknown"
    elif state == "bad-time":
        payload["sampled_at"] = "invalid"
    elif state == "bad-cores":
        payload["cpu_count"] = True
    elif state == "future":
        payload["reading_at"] = utc_iso(now + 60)
    elif state == "bad-lines":
        payload["lines"] = {"heavy": 5, "new_session": 4}
    elif state == "bad-level":
        payload["level"] = []
    if state not in {"missing", "malformed"}:
        atomic_write_json(path, payload)
    elif state == "malformed":
        path.parent.mkdir()
        path.write_text("{")
    result = CliRunner().invoke(cli, ["host", "load", "--json"])
    fresh = state in {"fresh", "transient-failure"}
    assert result.exit_code == (0 if fresh else 3), result.output
    decoded = json.loads(result.output)
    assert decoded.keys() == payload.keys()
    assert decoded["level"] == ("heavy_hold" if fresh else "unknown")
    assert decoded["path"] == str(path)
    if state == "fresh":
        assert decoded == payload


def test_host_load_text_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path))
    result = CliRunner().invoke(cli, ["host", "load"])
    assert result.exit_code == 3
    assert "level: unknown" in result.output
