"""Host-side cutover CLI: hold, drain, receipt, status and release.

Every command runs on the domain's host and talks to the owner's server with the
host's authenticated client. Output never carries credentials, prompts,
transcript text or feed pages.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import click
import httpx

from omnigent.usage_export.cutover import FEED_HARNESSES

EXIT_USAGE = 2
EXIT_CONFLICT = 3
EXIT_TIMEOUT = 4
EXIT_FAILED = 5
EXIT_CAS_REFUSED = 6
EXIT_UNAVAILABLE = 7
EXIT_LOCAL = 8


def _parse_domain(value: str) -> tuple[str, str]:
    host_id, _, harness = value.partition(",")
    host_id = host_id.strip()
    harness = harness.strip()
    if not host_id or not harness:
        raise click.BadParameter(
            "--domain takes <host_id>,<harness>, e.g. host_ab12,claude-sdk",
            param_hint="--domain",
        )
    if len(host_id.encode("utf-8")) > 256:
        raise click.BadParameter("host id exceeds 256 UTF-8 bytes", param_hint="--domain")
    if harness not in FEED_HARNESSES:
        raise click.BadParameter(
            f"harness must be one of {', '.join(sorted(FEED_HARNESSES))}",
            param_hint="--domain",
        )
    return host_id, harness


def _server_base(explicit: str | None) -> str:
    if explicit:
        return explicit.rstrip("/")
    for name in ("OMNIGENT_SERVER_URL", "OMNIGENT_API_URL"):
        value = os.environ.get(name)
        if value:
            return value.rstrip("/")
    from omnigent.host.local_server import local_server_url_if_healthy

    local = local_server_url_if_healthy()
    if local:
        return local.rstrip("/")
    raise click.ClickException(
        "No Omnigent server found. Pass --server, set OMNIGENT_SERVER_URL, "
        "or start one with `omnigent`."
    )


def _client_factory(base: str) -> httpx.AsyncClient:
    from omnigent.cli_auth import load_token, open_server_client

    return open_server_client(
        base,
        bearer_token=load_token(base),
        timeout=httpx.Timeout(30.0, read=None),
    )


def _fail(ctx: click.Context, code: str, message: str, exit_code: int) -> None:
    click.echo(f"{code}: {message}", err=True)
    ctx.exit(exit_code)


def _error_of(response: httpx.Response) -> tuple[str, str]:
    try:
        payload = response.json()
    except ValueError:
        return f"http_{response.status_code}", response.text[:200]
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict):
        return str(error.get("code", "error")), str(error.get("message", ""))
    return f"http_{response.status_code}", ""


def _raise_message(response: httpx.Response) -> str:
    code, message = _error_of(response)
    return f"{code}: {message}".strip(": ")


def _checkpoint_digest(sources: list[dict[str, Any]]) -> str:
    material = json.dumps(
        sorted((item["path"], item["offset"]) for item in sources),
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


async def _clock_offset_ms(client: httpx.AsyncClient, base: str) -> int:
    """Measured host minus server clock offset in milliseconds."""
    response = await client.get(f"{base}/v1/usage-events", params={"limit": 1})
    header = response.headers.get("date")
    if not header:
        return 0
    server_time = parsedate_to_datetime(header)
    if server_time.tzinfo is None:
        server_time = server_time.replace(tzinfo=timezone.utc)
    return int((time.time() - server_time.timestamp()) * 1000)


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _local_host_id() -> str | None:
    from omnigent.config import load_global_config

    try:
        config = load_global_config()
    except (OSError, ValueError):
        return None
    host = config.get("host") if isinstance(config, dict) else None
    if isinstance(host, dict) and isinstance(host.get("id"), str):
        return host["id"]
    return None


def _require_domain_host(ctx: click.Context, host_id: str) -> None:
    local = _local_host_id()
    if local is not None and local != host_id:
        _fail(
            ctx,
            "not_domain_host",
            f"this host is {local}, not {host_id}",
            EXIT_LOCAL,
        )


async def _request(
    client: httpx.AsyncClient,
    method: str,
    base: str,
    path: str,
    **kwargs: Any,
) -> httpx.Response:
    return await client.request(method, f"{base}{path}", **kwargs)


async def _hold(
    client: httpx.AsyncClient, base: str, host: str, harness: str, generation: int
) -> httpx.Response:
    return await _request(
        client,
        "POST",
        base,
        "/v1/usage-cutover/holds",
        json={"host_id": host, "harness": harness, "generation": generation},
    )


async def _status(client: httpx.AsyncClient, base: str, host: str, harness: str) -> dict[str, Any]:
    response = await _request(client, "GET", base, f"/v1/usage-cutover/holds/{host}/{harness}")
    if response.status_code != 200:
        raise click.ClickException(_raise_message(response))
    payload = response.json()
    return payload if isinstance(payload, dict) else {}


def _pending_calls(report: dict[str, Any]) -> list[dict[str, Any]]:
    """Host-side call window from the outbox's own unit ledger."""
    calls: list[dict[str, Any]] = []
    for unit in report.get("units", []):
        admission_id = unit.get("admission_id")
        observed = unit.get("occurred_at")
        calls.append(
            {
                "call_key": {
                    "meter_epoch": unit.get("meter_epoch"),
                    "producer_unit_id": unit.get("producer_unit_id"),
                },
                "server": None,
                "admission": {
                    "state": "linked" if isinstance(admission_id, str) else "unadmitted"
                },
                "admission_id": admission_id if isinstance(admission_id, str) else None,
                "occurred_at": observed,
                "ended_at": None,
                "state": "terminal_acked",
            }
        )
    return calls


async def _wait_terminal(
    client: httpx.AsyncClient,
    base: str,
    host: str,
    harness: str,
    spool: Any,
    timeout: float,
) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        snapshot = await _status(client, base, host, harness)
        report = spool.domain_report(harness)
        if snapshot.get("open_admissions") in (0, None) and not report["open_threads"]:
            return True
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(2.0)


async def _run_drain(
    *,
    base: str,
    host: str,
    harness: str,
    generation: int,
    timeout: float,
) -> tuple[int, dict[str, Any]]:
    from omnigent.usage_export.runtime import outbox

    spool = outbox()
    client = _client_factory(base)
    async with client:
        response = await _hold(client, base, host, harness, generation)
        if response.status_code != 200:
            return EXIT_CONFLICT, {"error": _raise_message(response)}
        offset = await _clock_offset_ms(client, base)
        if abs(offset) > 30_000:
            facts = {
                "detection": {
                    "checkpoint_digest": _checkpoint_digest([]),
                    "clock_offset_ms": offset,
                    "threshold_at": _iso_now(),
                },
                "admissions": [],
                "calls": [],
                "counts": {},
                "failure_codes": ["clock_skew"],
            }
            posted = await _request(
                client,
                "POST",
                base,
                f"/v1/usage-cutover/holds/{host}/{harness}/{generation}/receipts",
                json=facts,
            )
            return EXIT_FAILED, posted.json() if posted.status_code == 200 else facts
        checkpoint = _checkpoint_digest(spool.domain_report(harness)["sources"])
        terminal = await _wait_terminal(client, base, host, harness, spool, timeout)
        if not terminal:
            facts = {
                "detection": {
                    "checkpoint_digest": checkpoint,
                    "clock_offset_ms": offset,
                    "threshold_at": _iso_now(),
                },
                "admissions": [],
                "calls": _pending_calls(spool.domain_report(harness)),
                "counts": {
                    "open_threads": spool.domain_report(harness)["open_threads"],
                    "outbox_pending_observations": spool.domain_report(harness)[
                        "pending_observations"
                    ],
                },
                "timed_out": True,
            }
            posted = await _request(
                client,
                "POST",
                base,
                f"/v1/usage-cutover/holds/{host}/{harness}/{generation}/receipts",
                json=facts,
            )
            return EXIT_TIMEOUT, posted.json() if posted.status_code == 200 else facts
        for source in spool.domain_sources(harness):
            if source.is_file():
                spool.scan(source, final=True)
        for _ in range(1000):
            report = spool.domain_report(harness)
            if not report["pending_observations"]:
                break
            await spool.drain(client, limit=200, harness=harness)
        report = spool.domain_report(harness)
        facts = {
            "detection": {
                "checkpoint_digest": checkpoint,
                "clock_offset_ms": offset,
                "threshold_at": _iso_now(),
            },
            "admissions": [],
            "calls": _pending_calls(report),
            "counts": {
                "open_threads": report["open_threads"],
                "outbox_pending_observations": report["pending_observations"],
            },
        }
        posted = await _request(
            client,
            "POST",
            base,
            f"/v1/usage-cutover/holds/{host}/{harness}/{generation}/receipts",
            json=facts,
        )
        if posted.status_code != 200:
            return EXIT_FAILED, {"error": _raise_message(posted)}
        receipt = posted.json()
        result = receipt.get("result")
        if result == "drained":
            return 0, receipt
        if result == "timeout":
            return EXIT_TIMEOUT, receipt
        return EXIT_FAILED, receipt


@click.group("usage-export")
def usage_export() -> None:
    """Owner-scoped usage feed controls (cutover holds, bindings, route)."""


@usage_export.group("cutover")
def cutover() -> None:
    """Hold, drain, inspect and release one host×harness cutover domain."""


@cutover.command("status")
@click.option("--domain", "domain", required=True, help="<host_id>,<harness>")
@click.option("--server", "server", default=None, help="Omnigent server URL")
@click.option("--json", "as_json", is_flag=True, help="Print the raw JSON body")
def cutover_status(domain: str, server: str | None, as_json: bool) -> None:
    """Show the hold, its latest receipt and the domain's open counts."""
    host, harness = _parse_domain(domain)
    base = _server_base(server)

    async def run() -> dict[str, Any]:
        client = _client_factory(base)
        async with client:
            return await _status(client, base, host, harness)

    snapshot = asyncio.run(run())
    if as_json:
        click.echo(json.dumps(snapshot, indent=2, sort_keys=True))
        return
    latest = snapshot.get("latest_receipt") or {}
    click.echo(f"domain:   {host},{harness}")
    click.echo(f"state:    {snapshot.get('state')} (generation {snapshot.get('generation')})")
    click.echo(
        f"active:   {snapshot.get('active')}  open admissions: {snapshot.get('open_admissions')}"
    )
    if latest:
        click.echo(
            f"receipt:  seq {latest.get('receipt_seq')} {latest.get('kind')} "
            f"{latest.get('digest', '')[:16]}"
        )


@cutover.command("drain")
@click.option("--domain", "domain", required=True, help="<host_id>,<harness>")
@click.option("--generation", "generation", required=True, type=int)
@click.option("--timeout", "timeout", default=900.0, type=float, show_default=True)
@click.option("--receipt-out", "receipt_out", default=None, type=click.Path())
@click.option("--server", "server", default=None, help="Omnigent server URL")
@click.option("--json", "as_json", is_flag=True, help="Print the raw JSON body")
def cutover_drain(
    domain: str,
    generation: int,
    timeout: float,
    receipt_out: str | None,
    server: str | None,
    as_json: bool,
) -> None:
    """Hold the domain, wait for terminal turns, flush it and store a receipt."""
    host, harness = _parse_domain(domain)
    ctx = click.get_current_context()
    _require_domain_host(ctx, host)
    base = _server_base(server)
    exit_code, receipt = asyncio.run(
        _run_drain(
            base=base,
            host=host,
            harness=harness,
            generation=generation,
            timeout=timeout,
        )
    )
    if receipt_out:
        Path(receipt_out).write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    if as_json:
        click.echo(json.dumps(receipt, indent=2, sort_keys=True))
    else:
        click.echo(
            f"{receipt.get('result', 'failed')}: seq {receipt.get('receipt_seq')} "
            f"last_committed_sequence {receipt.get('last_committed_sequence')}"
        )
        for code in receipt.get("failure_codes", []):
            click.echo(f"  failure: {code}", err=True)
    if receipt.get("error"):
        click.echo(f"error: {receipt['error']}", err=True)
    ctx.exit(exit_code)


@cutover.command("release")
@click.option("--domain", "domain", required=True, help="<host_id>,<harness>")
@click.option("--generation", "generation", required=True, type=int)
@click.option("--cutover-at", "cutover_at", default=None, help="T: a whole UTC hour")
@click.option(
    "--consumer-receipt",
    "consumer_receipt",
    required=True,
    type=click.Path(exists=True, dir_okay=False),
)
@click.option("--abandon", "abandon", is_flag=True, help="Abandon the hold instead")
@click.option("--reason", "reason", default=None, help="Abandon reason code")
@click.option("--server", "server", default=None, help="Omnigent server URL")
@click.option("--json", "as_json", is_flag=True, help="Print the raw JSON body")
def cutover_release(
    domain: str,
    generation: int,
    cutover_at: str | None,
    consumer_receipt: str,
    abandon: bool,
    reason: str | None,
    server: str | None,
    as_json: bool,
) -> None:
    """CAS the release (or abandon) against the consumer's receipt."""
    host, harness = _parse_domain(domain)
    ctx = click.get_current_context()
    _require_domain_host(ctx, host)
    base = _server_base(server)
    try:
        consumer = json.loads(Path(consumer_receipt).read_text())
    except (OSError, ValueError) as exc:
        _fail(ctx, "invalid_consumer_receipt", str(exc), EXIT_USAGE)
        return
    body: dict[str, Any] = {"consumer_receipt": consumer}
    if abandon:
        if not reason:
            _fail(ctx, "reason_required", "--abandon needs --reason", EXIT_USAGE)
            return
        body.update({"abandon": True, "reason": reason})
    else:
        if not cutover_at:
            _fail(ctx, "cutover_at_required", "release needs --cutover-at", EXIT_USAGE)
            return
        body["cutover_at"] = cutover_at

    async def run() -> httpx.Response:
        client = _client_factory(base)
        async with client:
            return await _request(
                client,
                "POST",
                base,
                f"/v1/usage-cutover/holds/{host}/{harness}/{generation}/release",
                json=body,
            )

    response = asyncio.run(run())
    if response.status_code == 200:
        receipt = response.json()
        if as_json:
            click.echo(json.dumps(receipt, indent=2, sort_keys=True))
        else:
            click.echo(f"{receipt.get('kind')}: {receipt.get('result')}")
        return
    code, message = _error_of(response)
    click.echo(f"{code}: {message}", err=True)
    ctx.exit(EXIT_CAS_REFUSED if response.status_code == 409 else EXIT_UNAVAILABLE)


@usage_export.group("bindings")
def bindings() -> None:
    """Load and activate the owner's pinned account-binding revisions."""


@bindings.command("load")
@click.argument("document", type=click.Path(exists=True, dir_okay=False))
@click.option("--server", "server", default=None, help="Omnigent server URL")
@click.option("--json", "as_json", is_flag=True, help="Print the raw JSON body")
def bindings_load(document: str, server: str | None, as_json: bool) -> None:
    """Pin one binding document revision (owner auth)."""
    base = _server_base(server)
    try:
        payload = json.loads(Path(document).read_text())
    except (OSError, ValueError) as exc:
        raise click.ClickException(f"cannot read {document}: {exc}") from exc

    async def run() -> httpx.Response:
        client = _client_factory(base)
        async with client:
            return await _request(client, "POST", base, "/v1/usage-cutover/bindings", json=payload)

    response = asyncio.run(run())
    if response.status_code != 200:
        raise click.ClickException(_raise_message(response))
    result = response.json()
    if as_json:
        click.echo(json.dumps(result, indent=2, sort_keys=True))
    else:
        click.echo(
            f"revision {result.get('config_revision')} {result.get('digest')} "
            f"active={result.get('active')}"
        )


@bindings.command("activate")
@click.option("--revision", "revision", required=True, type=int)
@click.option("--digest", "digest", required=True)
@click.option("--server", "server", default=None, help="Omnigent server URL")
def bindings_activate(revision: int, digest: str, server: str | None) -> None:
    """Switch the active revision atomically."""
    base = _server_base(server)

    async def run() -> httpx.Response:
        client = _client_factory(base)
        async with client:
            return await _request(
                client,
                "POST",
                base,
                "/v1/usage-cutover/bindings/activate",
                json={"revision": revision, "digest": digest},
            )

    response = asyncio.run(run())
    if response.status_code != 200:
        raise click.ClickException(_raise_message(response))
    click.echo(f"active revision {revision}")


@usage_export.command("route")
@click.option("--harness", "harness", required=True)
@click.option("--server", "server", default=None, help="Omnigent server URL")
@click.option("--json", "as_json", is_flag=True, help="Print the raw JSON body")
def route(harness: str, server: str | None, as_json: bool) -> None:
    """Print the runner-attested billing route for one harness."""
    if harness not in FEED_HARNESSES:
        raise click.BadParameter(f"harness must be one of {', '.join(sorted(FEED_HARNESSES))}")
    base = _server_base(server)

    async def run() -> httpx.Response:
        client = _client_factory(base)
        async with client:
            return await _request(
                client,
                "GET",
                base,
                "/v1/usage-cutover/route",
                params={"harness": harness},
            )

    response = asyncio.run(run())
    if response.status_code != 200:
        raise click.ClickException(_raise_message(response))
    result = response.json()
    if as_json:
        click.echo(json.dumps(result, indent=2, sort_keys=True))
    elif result:
        click.echo(
            f"{result.get('host_id')} {harness}: {result.get('billing_route')} "
            f"{result.get('route_ref')}"
        )
    else:
        click.echo("no route attestation for this harness", err=True)
        sys.exit(0)
