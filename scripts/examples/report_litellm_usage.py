"""Push the current key's unmodified LiteLLM key-info response to Omnigent.

Set LITELLM_URL, LITELLM_API_KEY, OMNIGENT_URL, USAGE_KEY and USAGE_ACCOUNT.
Use --check-only to validate the response locally without writing to Omnigent.
Responses may contain credentials: never log or save the raw response.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ARG002
        return None


def request(url: str, key: str, body: bytes | None = None) -> bytes:
    # Avoid forwarding credentials to a redirect destination.
    opener = urllib.request.build_opener(NoRedirect)
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    with opener.open(req, timeout=30) as response:
        return response.read(5 * 1024 * 1024 + 1)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    try:
        observed = time.time()
        body = request(
            os.environ["LITELLM_URL"].rstrip("/") + "/key/info", os.environ["LITELLM_API_KEY"]
        )
        if len(body) > 5 * 1024 * 1024:
            raise ValueError("Response too large")
        payload = json.loads(body)
        # Validate using the exact same reader as the import route.
        from omnigent.usage.imports import ImportOptions, read_import

        report = read_import(
            "litellm-key", payload, ImportOptions(observed_at=observed), time.time()
        )
        if args.check_only:
            window = report.windows[0]
            print(
                json.dumps(
                    {
                        "check": "passed",
                        "windows": len(report.windows),
                        "kind": window.kind,
                        "unit": window.unit,
                        "has_limit": window.limit_value is not None,
                    }
                )
            )
            return 0
        account = os.environ["USAGE_ACCOUNT"]
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", account) is None:
            raise ValueError("Invalid account id")
        url = (
            os.environ["OMNIGENT_URL"].rstrip("/")
            + f"/v1/me/usage/accounts/{account}/import?format=litellm-key&observed_at={observed}"
        )
        # Key-info can include the raw key and provider configuration. Forward only
        # the documented budget fields; keep the vendor response's shape.
        safe_info = {
            name: payload["info"][name]
            for name in ("spend", "max_budget", "budget_duration", "budget_reset_at")
            if name in payload["info"]
        }
        safe_body = json.dumps({"info": safe_info}).encode()
        result = json.loads(request(url, os.environ["USAGE_KEY"], safe_body))
        print(
            json.dumps(
                {
                    "result": "accepted",
                    "applied": result["applied"],
                    "held": len(result["held"]),
                    "skipped": len(result["skipped"]),
                    "older_sections": result["older_sections"],
                }
            )
        )
        return 0
    except urllib.error.HTTPError as exc:
        print(f"Request failed (HTTP {exc.code}); response withheld.", file=sys.stderr)
    except (KeyError, ValueError, OSError):
        print(
            "Check configuration, connectivity and response format; details withheld.",
            file=sys.stderr,
        )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
