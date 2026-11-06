"""Tracked runtime resources must be covered by the wheel's package-data rules."""

from __future__ import annotations

import subprocess
import tomllib
from fnmatch import fnmatchcase
from glob import glob
from pathlib import Path


def test_tracked_runtime_data_is_packaged() -> None:
    root = Path(__file__).resolve().parents[1]
    with (root / "pyproject.toml").open("rb") as source:
        config = tomllib.load(source)["tool"]["setuptools"]
    discovery = config["packages"]["find"]
    tracked = (
        subprocess.check_output(["git", "ls-files", "-z", "--", "omnigent/"], cwd=root)
        .decode("utf-8")
        .split("\0")
    )
    assert any(tracked), "No tracked package files found"

    # These files are not runtime resources checked by this guard.
    exclusions = {
        "*.py": "Python modules are included by package discovery",
        "*.md": "Documentation/instructions have their own onboarding coverage",
        "*.proto": "Runtime uses the generated Python protobuf bindings",
        "*.pyi": "Type stubs are static metadata, included implicitly by setuptools",
        "*/py.typed": "Typing markers are included implicitly by setuptools",
        "*/tests/*": "Python test fixtures are not runtime resources",
        "*.test.*": "Node test sources are not runtime resources",
        "omnigent/server/static/web-ui/*": "Generated SPA is verified by the web build",
        "omnigent/inner/static/chat.html": "Legacy chat page is deliberately unpackaged",
        "omnigent/resources/examples/debby": "Directory symlink copied by setup.py",
        "omnigent/resources/examples/polly": "Directory symlink copied by setup.py",
    }
    expected = {
        name
        for name in tracked
        if name and not any(fnmatchcase(name, pattern) for pattern in exclusions)
    }
    packaged = set()
    for package, patterns in config["package-data"].items():
        if not any(fnmatchcase(package, rule) for rule in discovery["include"]):
            continue
        if any(fnmatchcase(package, rule) for rule in discovery.get("exclude", [])):
            continue
        package_dir = root.joinpath(*package.split("."))
        packaged.update(
            path.relative_to(root).as_posix()
            for pattern in patterns
            for path in map(Path, glob(str(package_dir / pattern), recursive=True))
            if path.is_file()
        )

    assert expected <= packaged, "Runtime data missing from setuptools package-data: " + ", ".join(
        sorted(expected - packaged)
    )
