"""A failed native launcher releases the bootstrap connection before exiting."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from omnigent.inner import pi_settings


@pytest.mark.parametrize("failure", ["sdk-import", "extension"])
def test_scoped_launcher_failure_closes_handoff(tmp_path: Path, monkeypatch, failure: str):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for the Pi launcher")
    sdk = tmp_path / "sdk" / "dist"
    modules = {
        "cli/setup.js": "export function setupCli() {}",
        "main.js": 'throw new Error("fixture SDK import failure");'
        if failure == "sdk-import"
        else "export async function main() {}",
        "core/resource-loader.js": "export class DefaultResourceLoader {}",
        "core/package-manager.js": "export class DefaultPackageManager {}",
        "core/settings-manager.js": "export class SettingsManager {}",
        "utils/paths.js": "export const resolvePath = path => path;",
    }
    for name, source in modules.items():
        file = sdk / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(source)
    (sdk.parent / "package.json").write_text('{"type":"module","version":"1.5.0"}')
    extension = tmp_path / "guard.cjs"
    extension.write_text('throw new Error("fixture extension load failure");')
    monkeypatch.setattr(pi_settings, "pi_cli_argv", lambda _: [str(sdk / "cli.js")])
    launcher, _ = pi_settings.ordered_pi_launch(
        "pi", [], extension=extension, scoped_handoff=tmp_path / "handoff"
    )
    client = Path(pi_settings.__file__).resolve().parent.parent / (
        "resources/pi_native/scoped_client.cjs"
    )
    closed = tmp_path / "closed"
    preload = tmp_path / "bootstrap-fixture.cjs"
    # The transport boundary holds the event loop just as the real watch does.
    preload.write_text(
        f"""
const fs = require("node:fs");
require.cache[{json.dumps(str(client))}] = {{exports: {{
  connectFromHandoff: async () => {{
    const watch = setInterval(() => {{}}, 1000);
    return {{connection: {{close() {{
      clearInterval(watch);
      fs.writeFileSync({json.dumps(str(closed))}, "closed");
    }}}}}};
  }}
}}}};
"""
    )
    try:
        result = subprocess.run(
            [node, "--require", str(preload), launcher],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except subprocess.TimeoutExpired:
        pytest.fail("failed launcher kept its bootstrap watch alive")
    assert closed.read_text() == "closed"
    assert result.returncode == 1
    assert result.stderr.strip() == (
        "Pi cannot prove permission extension ordering; check Pi SDK/extension errors."
    )
