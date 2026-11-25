"""Async queue-based telemetry emitter.

Errors are silently swallowed — telemetry must never disrupt the
application.  All opt-out signals are checked in :func:`is_disabled`.

Wire format (matches the API Gateway / Kinesis ingestion schema):

    POST <ingestion_url>
    {
        "records": [
            {
                "data": {
                    "event_name": "SessionCreatedEvent",
                    "session_id": "<telemetry-session-uuid>",
                    "omnigent_version": "0.4.2",
                    "schema_version": 1,
                    "python_version": "3.12.3",
                    "operating_system": "Linux",
                    "timestamp_ns": 1720000000000000000,
                    "status": "success",
                    "duration_ms": 0,
                    "installation_id": "<uuid>",
                    "environment": null,
                    "params": "{\"agent_id\": \"...\", ...}"
                },
                "partition-key": "<random-uuid>"
            }
        ]
    }

``session_id`` is a per-process UUID that groups all events from one
server run — it is NOT the Omnigent conversation id (which goes in
``params``).  ``params`` is a JSON-encoded string of event-specific
fields.  ``additionalProperties: false`` on the gateway means any field
not in the schema above will cause a 400, so event-specific data must
live in ``params``.

Remote config
~~~~~~~~~~~~~
On startup a daemon thread fetches a JSON config from a versioned URL::

    https://config.omnigent-telemetry.io/{version}.json          (prod)
    https://config-staging.omnigent-telemetry.io/{version}.json  (dev/pre-release)

The config shape::

    {
        "omnigent_version": "0.5.0",
        "ingestion_url": "https://...",   # required; disables telemetry if absent
        "disable_telemetry": false,       # kill-switch
        "disable_events": [],             # per-event kill-switch list
        "disable_os": [],                 # e.g. ["Windows"]
        "rollout_percentage": 100         # 0-100; probabilistic rollout
    }

If the config fetch fails or the config disables telemetry, the client
stops itself and drops all pending events.
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import platform
import queue
import random
import sys
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict, cast

if TYPE_CHECKING:
    from _typeshed import DataclassInstance

from omnigent.telemetry.environment import is_disabled_by_env
from omnigent.version import VERSION

_logger = logging.getLogger(__name__)

_BATCH_SIZE = 50
_BATCH_INTERVAL_S = 10.0
_MAX_QUEUE_SIZE = 512
_SCHEMA_VERSION = 1
_CONFIG_FETCH_TIMEOUT_S = 2.0

# Remote config base URLs. Dev/pre-release versions use staging.
_CONFIG_URL_PROD = "https://config.omnigent-telemetry.io"
_CONFIG_URL_STAGING = "https://config-staging.omnigent-telemetry.io"


# Cached result of is_disabled() — computed once on first call, then reused.
# Using a list so it's mutable from within the function (avoids global keyword).
_IS_DISABLED_CACHE: list[bool | None] = [None]


@dataclass
class TelemetryConfig:
    """Resolved remote configuration for the telemetry client."""

    ingestion_url: str
    disable_events: set[str] = field(default_factory=set)


class _TelemetryData(TypedDict):
    event_name: str
    session_id: str
    omnigent_version: str
    schema_version: int
    python_version: str
    operating_system: str
    timestamp_ns: int
    status: str
    duration_ms: int
    installation_id: str | None
    anon_user_id: str | None
    host_installation_id: str | None
    environment: str | None
    params: str | None


_TelemetryRecord = TypedDict(
    "_TelemetryRecord",
    {"data": _TelemetryData, "partition-key": str},
)


def _config_url() -> str:
    """Return the remote config URL for the running version.

    ``OMNIGENT_TELEMETRY_CONFIG_URL`` overrides for local testing.
    Dev/pre-release versions use the staging URL.
    """
    override = os.environ.get("OMNIGENT_TELEMETRY_CONFIG_URL", "").strip()
    if override:
        return f"{override.rstrip('/')}/{VERSION}.json"
    try:
        from packaging.version import Version

        v = Version(VERSION)
        if v.is_devrelease or v.is_prerelease:
            return f"{_CONFIG_URL_STAGING}/{VERSION}.json"
    except Exception:
        _logger.debug("Version parse failed; using production config URL", exc_info=True)
    return f"{_CONFIG_URL_PROD}/{VERSION}.json"


_config_fetch_guard: ContextVar[Callable[[], bool] | None] = ContextVar(
    "telemetry_config_fetch_guard", default=None
)


def _fetch_remote_config() -> TelemetryConfig | None:
    """Fetch and validate the remote telemetry config.

    :returns: :class:`TelemetryConfig` on success, ``None`` when
        telemetry should be disabled (fetch failure, kill-switch, OS
        exclusion, or rollout exclusion).
    """
    try:
        import urllib.request

        url = _config_url()
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        may_send = _config_fetch_guard.get()
        if may_send is not None and not may_send():
            return None
        with urllib.request.urlopen(req, timeout=_CONFIG_FETCH_TIMEOUT_S) as resp:
            raw_config: object = json.loads(resp.read().decode("utf-8"))

        if not isinstance(raw_config, dict):
            _logger.debug("Telemetry config is not an object; disabling telemetry")
            return None
        cfg: dict[object, object] = raw_config

        if cfg.get("omnigent_version") not in (VERSION, "default"):
            _logger.debug("Telemetry config version mismatch; disabling telemetry")
            return None
        if cfg.get("disable_telemetry") is True:
            _logger.debug("Telemetry disabled by remote config kill-switch")
            return None
        ingestion_url = cfg.get("ingestion_url")
        if not isinstance(ingestion_url, str) or not ingestion_url:
            _logger.debug("Telemetry config missing ingestion_url; disabling telemetry")
            return None
        disable_os_value = cfg.get("disable_os", [])
        disable_os = (
            {item for item in disable_os_value if isinstance(item, str)}
            if isinstance(disable_os_value, list)
            else set()
        )
        if platform.system() in disable_os:
            _logger.debug("Telemetry disabled for OS %s by remote config", platform.system())
            return None
        rollout_value = cfg.get("rollout_percentage", 100)
        rollout = (
            float(rollout_value)
            if isinstance(rollout_value, (int, float)) and not isinstance(rollout_value, bool)
            else 100.0
        )
        if random.random() * 100 >= rollout:
            _logger.debug("Telemetry excluded by rollout_percentage=%s", rollout)
            return None

        disable_events_value = cfg.get("disable_events", [])
        disable_events = (
            {item for item in disable_events_value if isinstance(item, str)}
            if isinstance(disable_events_value, list)
            else set()
        )

        return TelemetryConfig(
            ingestion_url=ingestion_url,
            disable_events=disable_events,
        )
    except Exception:
        _logger.debug("Telemetry config fetch failed; disabling telemetry", exc_info=True)
        return None


def config_disables_telemetry(config: Mapping[str, object]) -> bool:
    """Return ``True`` when a parsed config mapping sets top-level ``telemetry`` to false."""
    value = config.get("telemetry")
    # Some config loaders preserve YAML scalars as strings.
    return value is False or (isinstance(value, str) and value.strip().lower() == "false")


def _config_file_disables_telemetry(config_path: Path) -> bool:
    import yaml

    if not config_path.is_file():
        return False
    try:
        text = config_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return False
    try:
        data: object = yaml.safe_load(text)
    except yaml.YAMLError:
        # Preserve opt-out detection if a malformed YAML file still has a
        # `telemetry: false` directive.
        for line in text.splitlines():
            stripped = line.split("#", 1)[0].strip()
            key, sep, val = stripped.partition(":")
            if sep and key.strip().strip("'\"").lower() == "telemetry":
                return val.strip().lower() == "false"
        return False
    return isinstance(data, Mapping) and config_disables_telemetry(data)


def _config_telemetry_disabled() -> bool:
    """Return ``True`` when any active Omnigent config sets ``telemetry: false``.

    Uses the same config-path rules as ``omnigent.server.server_config``
    (``OMNIGENT_CONFIG`` or ``<server_data_dir>/config.yaml``) and
    ``omnigent.config`` (``OMNIGENT_CONFIG_HOME/config.yaml`` or
    ``~/.omnigent/config.yaml``), parsed with ``yaml.safe_load``.
    """
    try:
        from omnigent.config import global_config_path
        from omnigent.server.server_config import resolve_config_path

        candidates: list[Path] = []
        server_path = resolve_config_path()
        if server_path is not None:
            candidates.append(server_path)
        user_path = global_config_path(default_path=Path.home() / ".omnigent" / "config.yaml")
        if user_path not in candidates:
            candidates.append(user_path)
        return any(_config_file_disables_telemetry(path) for path in candidates)
    except Exception:
        return False


def is_disabled() -> bool:
    """Return ``True`` when telemetry should be completely suppressed.

    Result is cached after the first call — env vars and config.yaml are
    checked once at startup and not re-read on every emit, so there is no
    per-request I/O overhead.

    Checks (in order):
    0. ``OMNIGENT_ENABLE_TELEMETRY`` not set to ``1``/``true``/``yes`` (opt-in)
    1. ``OMNIGENT_ANALYTICS=0``
    2. ``DISABLE_TELEMETRY=true`` or ``OMNIGENT_DISABLE_TELEMETRY=true``
    3. ``DO_NOT_TRACK=1``
    4. Any CI or pytest environment marker
    5. ``telemetry: false`` in ``~/.omnigent/config.yaml``

    Always returns a ``bool``; never raises.
    """
    if _IS_DISABLED_CACHE[0] is not None:
        return _IS_DISABLED_CACHE[0]
    try:
        result = _compute_is_disabled()
    except Exception:
        result = True
    _IS_DISABLED_CACHE[0] = result
    return result


def _compute_is_disabled() -> bool:
    """Compute whether telemetry is disabled (uncached)."""
    if is_disabled_by_env(os.environ):
        return True
    return _config_telemetry_disabled()


def _detect_environment() -> str | None:
    """Return a short environment tag or ``None`` for plain installs."""
    try:
        checks: list[tuple[str, str]] = [
            ("KAGGLE_KERNEL_RUN_TYPE", "kaggle"),
            ("COLAB_BACKEND_VERSION", "colab"),
            ("AZUREML_ARM_WORKSPACE_NAME", "azure_ml"),
            ("SM_CURRENT_HOST", "sagemaker_studio"),
        ]
        for env_var, tag in checks:
            if os.environ.get(env_var):
                return tag
        # Docker: /.dockerenv exists in containers
        if os.path.exists("/.dockerenv"):
            return "docker"
        return None
    except Exception:
        return None


def _build_record(event: object) -> _TelemetryRecord:
    """Serialise *event* into the gateway ``data`` envelope.

    Event-specific fields (everything except ``installation_id``) are
    JSON-encoded into the ``params`` string so the gateway schema's
    ``additionalProperties: false`` constraint is satisfied.
    """
    from dataclasses import asdict

    fields: dict[str, object] = asdict(cast("DataclassInstance", event))
    installation_id_value = fields.pop("installation_id", None)
    installation_id = installation_id_value if isinstance(installation_id_value, str) else None
    session_id_value = fields.pop("session_id", None)
    session_id = session_id_value if isinstance(session_id_value, str) else None
    anon_user_id_value = fields.pop("anon_user_id", None)
    anon_user_id = anon_user_id_value if isinstance(anon_user_id_value, str) else None
    host_installation_id_value = fields.pop("host_installation_id", None)
    host_installation_id = (
        host_installation_id_value if isinstance(host_installation_id_value, str) else None
    )

    # All remaining event-specific fields go into params as a JSON string.
    params_str: str | None = None
    if fields:
        params_str = json.dumps(fields, default=str)

    data: _TelemetryData = {
        "event_name": type(event).__name__,
        "session_id": session_id or "",
        "omnigent_version": VERSION,
        "schema_version": _SCHEMA_VERSION,
        "python_version": sys.version.split()[0],
        "operating_system": platform.system(),
        "timestamp_ns": time.time_ns(),
        "status": "success",
        "duration_ms": 0,
        "installation_id": installation_id,
        "anon_user_id": anon_user_id,
        "host_installation_id": host_installation_id,
        "environment": _detect_environment(),
        "params": params_str,
    }
    return {
        "data": data,
        "partition-key": str(uuid.uuid4()),
    }


class TelemetryClient:
    """Fire-and-forget telemetry emitter backed by a background thread.

    On startup a daemon thread fetches the remote config (ingestion URL,
    kill-switches, rollout percentage).  The consumer thread buffers events
    until the config is resolved, then sends or discards them.  If the
    config fetch fails or signals ``disable_telemetry``, the client stops
    itself.

    All errors are suppressed — telemetry must never disrupt the application.
    """

    def __init__(self, *, enabled: Callable[[], bool] | None = None) -> None:
        self._enabled = enabled
        self._revoked = False
        self.config_skipped = False
        self._config: TelemetryConfig | None = None
        self._config_ready = threading.Event()
        self._queue: queue.Queue[_TelemetryRecord | None] = queue.Queue(maxsize=_MAX_QUEUE_SIZE)
        self._lock = threading.Lock()
        self._started = False
        self._stopped = False
        self._atexit_registered = False
        self._thread: threading.Thread | None = None
        self._config_thread: threading.Thread | None = None

    # ── Public interface ─────────────────────────────────

    def emit(self, event: object) -> None:
        """Queue an event for async delivery.

        Accepts any dataclass; converts it to the gateway wire format.
        Skips events listed in ``config.disable_events``.
        Silently no-ops when disabled or stopped.

        :param event: A dataclass instance, e.g. :class:`SessionCreatedEvent`.
        """
        if self._stopped:
            return
        # Defense-in-depth: re-check opt-out inside the client so
        # a late env-var change is respected even if the call site
        # skipped the module-level is_disabled() guard.
        if not self._may_send():
            return
        try:
            event_name = type(event).__name__
            # If config is already resolved, check per-event kill-switch.
            if self._config_ready.is_set() and self._config is not None:
                if event_name in self._config.disable_events:
                    return
            record = _build_record(event)
            self._ensure_started()
            try:
                self._queue.put_nowait(record)
            except queue.Full:
                pass  # queue full — drop event; telemetry must never block
        except Exception:
            _logger.debug("Telemetry emit failed; dropping event", exc_info=True)

    def flush(self) -> None:
        """Block until the queue is empty (used in tests and at shutdown)."""
        try:
            self._queue.join()
        except Exception:
            pass  # best-effort flush; never raise from telemetry

    def shutdown(self) -> None:
        """Signal the background thread to stop and wait briefly."""
        if self._stopped:
            return
        self._stopped = True
        try:
            self._queue.put_nowait(None)  # poison pill
        except Exception:
            pass  # queue may be full/closed; best-effort shutdown
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    # ── Internal helpers ─────────────────────────────────

    def revoke(self) -> None:
        """Discard this opt-in's queued records without waiting for the worker."""
        self._revoked = True
        self._stopped = True
        if self._atexit_registered:
            atexit.unregister(self._atexit_callback)
            self._atexit_registered = False

    def _may_send(self) -> bool:
        if self._revoked:
            return False
        return self._enabled() if self._enabled is not None else not is_disabled()

    def _ensure_started(self) -> None:
        if self._started:
            return
        with self._lock:
            if self._started:
                return
            # Config fetch runs in its own daemon thread so it never blocks
            # the first emit call.
            self._config_thread = threading.Thread(
                target=self._load_config,
                name="OmnigentTelemetryConfig",
                daemon=True,
            )
            self._config_thread.start()

            self._thread = threading.Thread(
                target=self._consumer,
                name="OmnigentTelemetryConsumer",
                daemon=True,
            )
            self._thread.start()
            self._started = True
            if not self._atexit_registered:
                atexit.register(self._atexit_callback)
                self._atexit_registered = True

    def _load_config(self) -> None:
        """Daemon thread: fetch remote config then signal the consumer."""
        try:
            if self._enabled is not None and not self._may_send():
                self.config_skipped = True
                return
            token = _config_fetch_guard.set(self._may_send if self._enabled is not None else None)
            try:
                cfg = _fetch_remote_config()
            finally:
                _config_fetch_guard.reset(token)
            if cfg is None and self._enabled is not None and not self._may_send():
                self.config_skipped = True
                return
            if cfg is None:
                # Kill-switch or fetch failure — stop the client.
                self._stopped = True
                try:
                    self._queue.put_nowait(None)  # unblock consumer
                except queue.Full:
                    pass
            else:
                self._config = cfg
        except Exception:
            _logger.debug("Telemetry config load failed; stopping client", exc_info=True)
            self._stopped = True
        finally:
            self._config_ready.set()

    def _atexit_callback(self) -> None:
        try:
            self.shutdown()
        except Exception:
            pass  # best-effort shutdown at process exit; telemetry must never disrupt termination

    def _consumer(self) -> None:
        """Background thread: wait for config, then drain the queue in batches."""
        # Wait for config to be resolved before sending anything.
        self._config_ready.wait()

        if self._stopped or self._config is None:
            # Config fetch failed or kill-switched — drain and discard.
            while not self._queue.empty():
                try:
                    self._queue.get_nowait()
                    self._queue.task_done()
                except queue.Empty:
                    break
            return

        ingestion_url = self._config.ingestion_url
        disable_events = self._config.disable_events
        pending: list[_TelemetryRecord] = []
        last_flush = time.monotonic()

        while not self._stopped:
            try:
                item = self._queue.get(timeout=1.0)
            except queue.Empty:
                if pending and time.monotonic() - last_flush >= _BATCH_INTERVAL_S:
                    self._send(pending, ingestion_url)
                    pending = []
                    last_flush = time.monotonic()
                continue

            if item is None:
                # Poison pill — flush what we have, then exit.
                if pending:
                    self._send(pending, ingestion_url)
                self._queue.task_done()
                break

            # Apply per-event kill-switch at send time too (config may have
            # arrived after the event was queued).
            event_name = item["data"]["event_name"]
            if event_name not in disable_events:
                pending.append(item)
            self._queue.task_done()

            if len(pending) >= _BATCH_SIZE or time.monotonic() - last_flush >= _BATCH_INTERVAL_S:
                self._send(pending, ingestion_url)
                pending = []
                last_flush = time.monotonic()

        # Drain remaining on stop.
        while not self._queue.empty():
            try:
                item = self._queue.get_nowait()
                if item is not None:
                    event_name = item["data"]["event_name"]
                    if event_name not in disable_events:
                        pending.append(item)
                self._queue.task_done()
            except queue.Empty:
                break
        if pending:
            self._send(pending, ingestion_url)

    def _send(self, records: list[_TelemetryRecord], ingestion_url: str) -> None:
        """POST a batch to the ingestion endpoint."""
        if not records:
            return
        try:
            import urllib.request

            body = json.dumps({"records": records}).encode("utf-8")
            req = urllib.request.Request(
                ingestion_url,
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            if self._enabled is not None and not self._may_send():
                return
            with urllib.request.urlopen(req, timeout=3) as resp:
                resp.read()
        except Exception:
            _logger.debug("Telemetry send failed; dropping batch", exc_info=True)


# ── Module-level singleton ───────────────────────────────

_CLIENT: TelemetryClient | None = None
_CLIENT_LOCK = threading.Lock()


class ServerUsageTelemetry:
    """One anonymous usage emitter per app, with a fresh queue on re-enabling."""

    def __init__(self, enabled: Callable[[], bool]) -> None:
        self._enabled = enabled
        self._client: TelemetryClient | None = None
        self._lock = threading.Lock()

    def refresh(self) -> TelemetryClient | None:
        with self._lock:
            if not self._enabled():
                self._revoke()
                return None
            if self._client is None or self._client.config_skipped:
                self._revoke()
                _prime_installation_id()
                try:
                    client = TelemetryClient(enabled=self._enabled)
                    client._ensure_started()
                    self._client = client
                except Exception:
                    _logger.debug("Server usage telemetry init failed", exc_info=True)
            return self._client

    def close(self) -> None:
        with self._lock:
            self._revoke()

    def _revoke(self) -> None:
        if self._client is not None:
            self._client.revoke()
            self._client = None

    @contextmanager
    def scope(self) -> Iterator[None]:
        token = _server_usage_telemetry.set(self)
        try:
            yield
        finally:
            _server_usage_telemetry.reset(token)


_server_usage_telemetry: ContextVar[ServerUsageTelemetry | None] = ContextVar(
    "server_usage_telemetry", default=None
)


def get_client() -> TelemetryClient | None:
    """Return the active usage client, or ``None`` when disabled."""
    server_telemetry = _server_usage_telemetry.get()
    if server_telemetry is not None:
        return server_telemetry.refresh()
    return _CLIENT


def _prime_installation_id() -> None:
    # Cache the installation ID before request handlers need it.
    try:
        from omnigent.telemetry.installation_id import get_installation_id

        get_installation_id()
    except Exception:
        _logger.debug("Telemetry installation-id prime failed", exc_info=True)


def init_client(*, config: Mapping[str, object] | None = None) -> None:
    """Initialise the module-level client if telemetry is enabled.

    Safe to call multiple times; idempotent after the first call.

    :param config: Optional parsed server config dict (e.g. from ``-c
        config.yaml``).  When ``config.get("telemetry") is False``
        telemetry is disabled regardless of env vars.
    """
    global _CLIENT
    if is_disabled():
        return
    if config is not None and config.get("telemetry") is False:
        return
    _prime_installation_id()
    with _CLIENT_LOCK:
        if _CLIENT is None:
            try:
                _CLIENT = TelemetryClient()
                # Start threads eagerly at init time so the config fetch
                # runs in the background before the first event arrives.
                _CLIENT._ensure_started()
            except Exception:
                _logger.debug("TelemetryClient init failed", exc_info=True)


def emit(event: object) -> None:
    """Emit an event through the module-level client.

    No-op when telemetry is disabled or the client is not initialised.
    Never raises.

    :param event: A dataclass instance.
    """
    try:
        if _server_usage_telemetry.get() is None and is_disabled():
            return
        client = get_client()
        if client is not None:
            client.emit(event)
    except Exception:
        _logger.debug(
            "Telemetry emit failed; swallowing to avoid disrupting application", exc_info=True
        )
