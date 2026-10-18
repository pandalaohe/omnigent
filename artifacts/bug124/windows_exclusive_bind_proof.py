"""Run with the installed Python on Windows; exit 0 proves both expectations.

Uses only stdlib and 127.0.0.1. The parent keeps a listening socket open while
a second Python process attempts to bind and listen on that same address.
"""

from __future__ import annotations

import argparse
import json
import socket
import subprocess
import sys
from pathlib import Path


def listener(mode: str) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        option = (
            socket.SO_REUSEADDR if mode == "old" else socket.SO_EXCLUSIVEADDRUSE  # pyrefly: ignore[missing-attribute]
        )
        sock.setsockopt(socket.SOL_SOCKET, option, 1)
    except BaseException:
        sock.close()
        raise
    return sock


def child_result(mode: str, port: int) -> dict[str, object]:
    with listener(mode) as sock:
        try:
            sock.bind(("127.0.0.1", port))
            sock.listen(1)
        except OSError as exc:
            return {
                "bound": False,
                "winerror": exc.winerror,  # pyrefly: ignore[missing-attribute]
                "error": str(exc),
            }
    return {"bound": True}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--child", choices=("old", "exclusive"))
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()
    if sys.platform != "win32":
        parser.exit(2, "Windows required; no socket test was run.\n")
    if args.child:
        print(json.dumps(child_result(args.child, args.port)))
        return 0

    passed = True
    print(f"Python: {sys.version}; executable: {sys.executable}")
    for mode in ("old", "exclusive"):
        with listener(mode) as first:
            first.bind(("127.0.0.1", 0))
            first.listen(1)
            port = first.getsockname()[1]
            second = subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "--child",
                    mode,
                    "--port",
                    str(port),
                ],
                capture_output=True,
                text=True,
                check=True,
                timeout=15,
                creationflags=subprocess.CREATE_NO_WINDOW,  # pyrefly: ignore[missing-attribute]
            )
            result = json.loads(second.stdout)
            expected = (
                result.get("bound") is True
                if mode == "old"
                else result.get("bound") is False and result.get("winerror") in (10013, 10048)
            )
            passed = passed and expected
            print(
                json.dumps(
                    {
                        "mode": mode,
                        "port": port,
                        "first_bound": True,
                        "second": result,
                        "pass": expected,
                    }
                )
            )
    print("PASS" if passed else "FAIL")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
