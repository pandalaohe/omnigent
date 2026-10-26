"""Load classifier scenarios from the host-local scheduling contract."""

from pathlib import Path

import pytest

from omnigent.host.load_level import FACTORY_LINES, LevelState, LoadLines, load_equiv
from omnigent.util.atomic_json import atomic_write_json


def _buckets(
    state: LevelState, loads: list[float | None], *, start: int = 0, memory: float = 50
) -> list[tuple[str, str | None]]:
    results = []
    for index, load in enumerate(loads, start):
        if load is not None:
            state.add_sample(index * 60 + 1, load, memory)
        results.append(state.step((index + 1) * 60, FACTORY_LINES))
    return results


@pytest.mark.parametrize(
    ("loads", "memory", "expected"),
    [
        ([4.2, 4.5, 4.1], 50, ("heavy_hold", "load")),
        ([3.0, 6.0, 3.1], 50, ("normal", None)),
        ([0.5] * 3, 92, ("heavy_hold", "memory")),
        ([0.5] * 3, 50, ("spare", None)),
        ([4.2, None, 4.3, 4.4], 50, ("normal", None)),
    ],
)
def test_completed_minute_flags(
    loads: list[float | None], memory: float, expected: tuple[str, str | None]
) -> None:
    assert _buckets(LevelState(), loads, memory=memory)[-1] == expected


def test_constant_overload_and_clear_across_bands() -> None:
    state = LevelState()
    assert _buckets(state, [6] * 6)[2:] == [("new_session_hold", "load")] * 4
    assert _buckets(state, [4.5, 3, 0.5], start=6)[-1] == ("heavy_hold", "load")
    assert _buckets(state, [3], start=9)[-1] == ("normal", None)


def test_bucket_mean_and_no_partial_bucket_changes() -> None:
    state = LevelState()
    for minute in range(3):
        state.add_sample(minute * 60 + 1, 6, 50)
        state.add_sample(minute * 60 + 30, 2, 50)
        assert state.step(minute * 60 + 59, FACTORY_LINES) == ("normal", None)
    assert state.step(180, FACTORY_LINES) == ("heavy_hold", "load")


def test_sensor_failure_resets_flags_and_requires_new_run() -> None:
    state = LevelState()
    _buckets(state, [4.2] * 3)
    assert state.step(321, FACTORY_LINES) == ("unknown", None)
    assert _buckets(state, [4.2] * 2, start=6)[-1] == ("normal", None)
    assert _buckets(state, [4.2], start=8)[-1] == ("heavy_hold", "load")


def test_invalid_readings_do_not_keep_a_hold_alive() -> None:
    state = LevelState()
    _buckets(state, [6] * 3)
    state.add_sample(400, float("nan"), 50)
    state.add_sample(400, 6, float("nan"))
    assert state.step(400, FACTORY_LINES) == ("unknown", None)


def test_resuming_after_a_stall_resets_old_hold() -> None:
    state = LevelState()
    _buckets(state, [6] * 3)
    state.add_sample(400, 6, 50)
    assert state.step(400, FACTORY_LINES) == ("normal", None)


def test_windows_equivalent_and_missing_inputs() -> None:
    assert load_equiv(2, 50, 24, windows=True) == 14
    assert load_equiv(2, None, None, windows=False) == 2
    assert load_equiv(None, 50, 24, windows=True) is None
    assert load_equiv(2, None, 24, windows=True) is None
    assert load_equiv(2, 50, None, windows=True) is None


@pytest.mark.parametrize("values", [(5, 4, 90), (0, 5, 90), (4, 65, 90), (4, 5, float("nan"))])
def test_invalid_lines_rejected(values: tuple[float, float, float]) -> None:
    with pytest.raises(ValueError):
        LoadLines(*values)


def test_atomic_json_replaces_and_refuses_symlink(tmp_path: Path) -> None:
    path = tmp_path / "run" / "load.json"
    atomic_write_json(path, {"old": 1})
    atomic_write_json(path, {"new": 2})
    assert path.read_text() == '{"new": 2}'
    link = tmp_path / "link.json"
    link.symlink_to(path)
    with pytest.raises(OSError, match="symlinked"):
        atomic_write_json(link, {"bad": 3})
    assert path.read_text() == '{"new": 2}'
    assert not list(path.parent.glob("*.tmp"))
