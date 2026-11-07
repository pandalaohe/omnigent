"""Private process-attestor broker; ordinary Pi scopes cannot install proofs."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import secrets
import stat
from collections.abc import Iterator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast
from urllib.parse import urlsplit
from uuid import uuid4

import psutil

from omnigent._platform import IS_WINDOWS
from omnigent.permission_tiers import (
    NativeCallBinding,
    NativeInputSnapshot,
    NativeSupport,
    NativeSupportStatus,
)
from omnigent.permission_tiers import _native_digest as native_digest
from omnigent.runner.pi_scopes import PiScopeAdmission
from omnigent.util.atomic_json import atomic_write_json

if TYPE_CHECKING:
    from http.server import HTTPServer

    from omnigent.runner.permission_tiers import PermissionTierRuntime


logger = logging.getLogger(__name__)


@contextmanager
def _bootstrap_check(check: str) -> Iterator[None]:
    """Name the failed broker boundary without logging private exception contents."""
    try:
        yield
    except Exception as exc:
        logger.warning("Pi bootstrap rejected: check=%s exception=%s", check, type(exc).__name__)
        raise


def _reject(check: str, message: str) -> ValueError:
    logger.warning("Pi bootstrap rejected: check=%s exception=ValueError", check)
    return ValueError(message)


def process_identity(pid: int) -> tuple[int, float] | None:
    try:
        process = psutil.Process(pid)
        if not process.is_running() or process.status() == psutil.STATUS_ZOMBIE:
            return None
        return pid, process.create_time()
    except (psutil.Error, OSError):
        return None


@dataclass
class PiAttestorProcess:
    pid: int
    identity: tuple[int, float]
    root: bool
    launch_epoch: str
    token: str = field(repr=False)
    initial_root: PiScopeAdmission | None = field(default=None, repr=False)
    root_loader: str | None = None
    installations: dict[str, PiScopeAdmission] = field(default_factory=dict, repr=False)
    loaders: dict[str, str] = field(default_factory=dict, repr=False)
    sequence: int = 0
    previous_digest: str = ""
    previous_reply: dict[str, object] = field(default_factory=dict, repr=False)
    closed: bool = False
    handoff: Path | None = field(default=None, repr=False)


@dataclass
class PiRootProcessLaunch:
    launch_id: str
    handoff: Path
    actor: PiAttestorProcess | None = None
    status: Literal["reserved", "published", "revoked"] = "reserved"


class PiBootstrapBroker:
    """One runner-loop ledger backed by the existing scope and permission registries."""

    def __init__(self, runtime: PermissionTierRuntime, session_id: str) -> None:
        self.runtime, self.session_id = runtime, session_id
        self.incarnation = uuid4().hex
        self.endpoint: str | None = None
        self._actors: dict[str, PiAttestorProcess] = {}
        self._proofs: dict[tuple[str, str], NativeSupport] = {}
        self._launcher_capability: str | None = None
        self._root_process_launch: PiRootProcessLaunch | None = None
        self._unsubscribe = runtime.pi_scopes.observe_launch_invalidation(self._launch_invalidated)

    def _launch_invalidated(self, root_session_id: str) -> None:
        if root_session_id == self.session_id:
            self.invalidate()

    def close(self) -> None:
        self.endpoint = None
        self._launcher_capability = None
        self._root_process_launch = None
        self.invalidate()
        self._unsubscribe()

    def launcher_descriptor(self) -> dict[str, object]:
        """Delivered only to the authenticated Python harness, never to a Pi scope."""
        state = self.runtime.state(self.session_id)
        if (
            self.endpoint is None
            or self.runtime.observation_harness(self.session_id) not in ("pi", "pi-native")
            or state is None
            or state.status not in ("ready", "blocked")
            or state.tier == "full"
        ):
            raise ValueError("Pi root launcher unavailable")
        self.invalidate()
        self._launcher_capability = "pi_attestor_" + secrets.token_urlsafe(32)
        self._root_process_launch = None
        return {
            "protocol": 1,
            "session_id": self.session_id,
            "endpoint": self.endpoint,
            "process_capability": self._launcher_capability,
        }

    def authenticate_launcher(self, authorization: str | None) -> None:
        state = self.runtime.state(self.session_id)
        if (
            self.endpoint is None
            or self._launcher_capability is None
            or not isinstance(authorization, str)
            or len(authorization) > 256
            or not hmac.compare_digest(
                authorization.encode(), ("Bearer " + self._launcher_capability).encode()
            )
            or self.runtime.observation_harness(self.session_id) not in ("pi", "pi-native")
            or state is None
            or state.status not in ("ready", "blocked")
            or state.tier == "full"
        ):
            raise _reject("launcher_authentication", "Pi root launcher authentication unavailable")

    def handle_launcher(self, payload: dict[str, object]) -> dict[str, object]:
        """Reserve/publish/confirm the exact subprocess observed by the trusted harness."""
        common = {"protocol", "op", "launch_id"}
        op, launch_id = payload.get("op"), payload.get("launch_id")
        fields = {
            "reserve": common,
            "publish": common | {"pid", "started_at"},
            "confirm": common,
            "revoke": common,
        }
        if (
            type(payload.get("protocol")) is not int
            or payload["protocol"] != 1
            or not isinstance(op, str)
            or op not in fields
            or set(payload) != fields[op]
            or not isinstance(launch_id, str)
            or re.fullmatch(r"[0-9a-f]{32}", launch_id) is None
        ):
            raise _reject("launcher_request_fields", "Pi root launcher request unavailable")
        launch = self._root_process_launch
        if op == "reserve":
            if launch is None or launch.launch_id != launch_id:
                if launch is not None and launch.actor is not None and self.live(launch.actor):
                    raise _reject(
                        "launcher_process_owned",
                        "Pi root process is still owned by another launch",
                    )
                launch = PiRootProcessLaunch(launch_id, self.reserve_handoff())
                self._root_process_launch = launch
            if launch.status != "reserved":
                raise _reject(
                    "launcher_reservation_pending",
                    "Pi root handoff reservation is no longer pending",
                )
            reply: dict[str, object] = {"handoff": str(launch.handoff)}
        else:
            if launch is None or launch.launch_id != launch_id:
                raise _reject("launcher_generation", "Pi root launcher generation changed")
            if op == "revoke":
                if launch.actor is not None:
                    self.revoke_process(launch.actor)
                launch.status = "revoked"
                reply = {"revoked": True}
            elif op == "publish":
                pid, started_at = payload.get("pid"), payload.get("started_at")
                if type(pid) is not int or type(started_at) not in (int, float):
                    raise _reject(
                        "launcher_identity_fields", "Pi root process identity unavailable"
                    )
                identity = (cast(int, pid), cast(float, started_at))
                if launch.status == "reserved":
                    actor = self.register_process(
                        identity[0], expected_identity=identity, root=True
                    )
                    launch.actor = actor
                    try:
                        self.publish_handoff(actor, launch.handoff)
                    except BaseException:
                        self.revoke_process(actor)
                        launch.status = "revoked"
                        raise
                    launch.status = "published"
                if (
                    launch.status != "published"
                    or launch.actor is None
                    or launch.actor.identity != identity
                    or not self.live(launch.actor)
                ):
                    raise _reject(
                        "launcher_publication_live", "Pi root process publication is stale"
                    )
                reply = {"published": True}
            else:
                if (
                    launch.status != "published"
                    or launch.actor is None
                    or not self.live(launch.actor)
                ):
                    raise _reject("launcher_confirmation_live", "Pi root process is not published")
                with _bootstrap_check("launcher_parent_scope"):
                    parent = self.runtime.pi_scopes.parent_scope(self.session_id)
                with _bootstrap_check("launcher_scope_acknowledgement"):
                    ready = parent.status == "ready" and self.runtime.acknowledge_pi_scope(
                        self.session_id, parent.admission.credential
                    )
                reply = {"ready": ready}
        return {**reply, "launch_id": launch_id}

    def frame(self, actor: PiAttestorProcess) -> dict[str, object]:
        if not self.live(actor):
            raise ValueError("Pi bootstrap continuity unavailable")
        revoked = [
            admission.principal.scope_id
            for admission in actor.installations.values()
            if self.runtime.pi_scopes.record(admission.principal).status == "revoked"
        ]
        return {
            "live": True,
            "incarnation": self.incarnation,
            "launch_epoch": actor.launch_epoch,
            "revoked_scopes": revoked,
        }

    def attach_listener(self, server: HTTPServer, advertised_url: str) -> None:
        """Called with the owned listener, not an endpoint supplied by a request."""
        self.endpoint = None
        address = server.socket.getsockname()
        parsed = urlsplit(advertised_url)
        if (
            not ipaddress.ip_address(address[0]).is_loopback
            or parsed.scheme != "http"
            or parsed.hostname not in ("127.0.0.1", "::1")
            or parsed.hostname != address[0]
            or parsed.port != address[1]
            or parsed.username
            or parsed.password
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Pi bootstrap requires the owned loopback listener")
        self.endpoint = advertised_url.rstrip("/")

    def register_process(
        self, pid: int, *, expected_identity: tuple[int, float], root: bool
    ) -> PiAttestorProcess:
        """Private launcher entry; no wire operation registers or chooses a process."""
        if (
            self.endpoint is None
            or type(pid) is not int
            or pid <= 1
            or process_identity(pid) != expected_identity
        ):
            raise _reject(
                "process_identity_or_endpoint", "Pi bootstrap process or endpoint unavailable"
            )
        admission: PiScopeAdmission | None = None
        if root:
            self.invalidate()
            with _bootstrap_check("process_scope_launch"):
                admission = self.runtime.begin_pi_scope_launch(self.session_id, native_guard=True)
            epoch = admission.principal.launch_epoch
        else:
            epochs = {a.launch_epoch for a in self._actors.values() if a.root and not a.closed}
            if len(epochs) != 1:
                raise _reject("process_root_epoch", "Pi bootstrap root process unavailable")
            epoch = next(iter(epochs))
        if len(self._actors) >= 4096:
            raise _reject("process_limit", "Pi bootstrap process limit reached")
        token = "pi_attestor_" + secrets.token_urlsafe(32)
        actor = PiAttestorProcess(
            pid, expected_identity, root, epoch, token, initial_root=admission if root else None
        )
        self._actors[hashlib.sha256(token.encode()).hexdigest()] = actor
        return actor

    def reserve_handoff(self) -> Path:
        """Private launcher locator, allocated before the Node process exists."""
        with _bootstrap_check("handoff_directory_io"):
            directory = self.runtime.path(self.session_id).parent.resolve() / "pi-bootstrap"
            directory.mkdir(mode=0o700, exist_ok=True)
            info = directory.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            # Windows protects this per-user directory through its ACL, not POSIX bits.
            or (not IS_WINDOWS and stat.S_IMODE(info.st_mode) & 0o077)
            or (hasattr(os, "getuid") and info.st_uid != os.getuid())
        ):
            raise _reject(
                "handoff_directory_permissions", "Pi bootstrap handoff directory unavailable"
            )
        return directory / (uuid4().hex + ".json")

    def publish_handoff(self, actor: PiAttestorProcess, destination: Path) -> None:
        """Deliver directly to the independent preload, never through tool results."""
        with _bootstrap_check("handoff_destination_resolve"):
            directory = self.runtime.path(self.session_id).parent.resolve() / "pi-bootstrap"
        with _bootstrap_check("handoff_destination_metadata"):
            unavailable = (
                not self.live(actor)
                or actor.handoff is not None
                or destination.parent != directory
                or destination.exists()
                or destination.is_symlink()
                or not re.fullmatch(r"[0-9a-f]{32}\.json", destination.name)
            )
        if unavailable:
            raise _reject("handoff_destination", "Pi bootstrap handoff unavailable")
        # Revalidate ownership and no-follow posture at publication as well as reservation.
        self.reserve_handoff()
        with _bootstrap_check("handoff_atomic_write"):
            atomic_write_json(
                destination,
                {
                    "protocol": 1,
                    "pid": actor.pid,
                    "endpoint": self.endpoint,
                    "credential": actor.token,
                    "brokerIncarnation": self.incarnation,
                    "launchEpoch": actor.launch_epoch,
                    "statePath": str(self.runtime.path(self.session_id)),
                    "bridgeDir": str(directory.parent),
                },
            )
        actor.handoff = destination

    def authenticate(self, authorization: str | None) -> PiAttestorProcess:
        if not isinstance(authorization, str) or not authorization.startswith("Bearer "):
            raise ValueError("Pi bootstrap authentication unavailable")
        value = authorization.removeprefix("Bearer ")
        if not 1 <= len(value) <= 256:
            raise ValueError("Pi bootstrap authentication unavailable")
        actor = self._actors.get(hashlib.sha256(value.encode()).hexdigest())
        if actor is None or not hmac.compare_digest(value, actor.token) or not self.live(actor):
            raise ValueError("Pi bootstrap authentication unavailable")
        return actor

    def live(self, actor: PiAttestorProcess) -> bool:
        if actor.closed or self.endpoint is None:
            return False
        if process_identity(actor.pid) != actor.identity:
            self.revoke_process(actor)
            return False
        return True

    def revoke_process(self, actor: PiAttestorProcess) -> None:
        if actor.closed:
            return
        actor.closed = True
        if actor.handoff is not None:
            with suppress(FileNotFoundError):
                actor.handoff.unlink()
        admissions = list(actor.installations.values())
        if actor.initial_root is not None:
            admissions.append(actor.initial_root)
        for admission in admissions:
            with suppress(ValueError):
                self.runtime.revoke_pi_scope(admission.principal)

    def invalidate(self) -> None:
        actors = tuple(self._actors.values())
        self._actors.clear()
        for actor in actors:
            self.revoke_process(actor)
        self._proofs.clear()

    def handle(self, actor: PiAttestorProcess, payload: dict[str, object]) -> dict[str, object]:
        """Only the authenticated control adapter calls this after decoding its body."""
        if (
            not self.live(actor)
            or payload.get("protocol") != 1
            or type(payload.get("protocol")) is not int
            or type(payload.get("sequence")) is not int
        ):
            raise ValueError("Pi bootstrap state unavailable")
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        sequence = cast(int, payload["sequence"])
        if sequence == actor.sequence and digest == actor.previous_digest:
            return actor.previous_reply
        if sequence != actor.sequence + 1:
            raise ValueError("Pi bootstrap sequence unavailable")
        op = payload.get("op")
        fields = {
            "bind": {"protocol", "sequence", "op", "loader", "handle", "registry"},
            "attest": {
                "protocol",
                "sequence",
                "op",
                "handle",
                "call_id",
                "name",
                "arguments",
                "revision",
                "support",
            },
            "revoke": {"protocol", "sequence", "op", "handle"},
        }
        if not isinstance(op, str) or op not in fields or set(payload) - fields[op]:
            raise ValueError("Pi bootstrap operation unavailable")
        handle = payload.get("handle")
        if not isinstance(handle, str) or not 1 <= len(handle) <= 128:
            raise ValueError("Pi bootstrap installation unavailable")
        reply: dict[str, object]
        if op == "bind":
            reply = self._bind(actor, handle, payload)
        else:
            admission = actor.installations.get(handle)
            if admission is None:
                raise ValueError("Pi bootstrap installation unavailable")
            if op == "revoke":
                self.runtime.revoke_pi_scope(admission.principal)
                reply = {
                    "revoked": True,
                    "scope_id": admission.principal.scope_id,
                    "installation_id": admission.principal.installation_id,
                }
            else:
                reply = self._attest(admission, payload)
        actor.sequence, actor.previous_digest, actor.previous_reply = sequence, digest, reply
        return reply

    def _bind(
        self, actor: PiAttestorProcess, handle: str, payload: dict[str, object]
    ) -> dict[str, object]:
        loader = payload.get("loader")
        if not isinstance(loader, str) or not 1 <= len(loader) <= 128:
            raise ValueError("Pi bootstrap loader unavailable")
        admission = actor.installations.get(handle)
        if admission is None:
            for previous_handle, previous_loader in actor.loaders.items():
                if previous_loader == loader:
                    self.runtime.revoke_pi_scope(actor.installations[previous_handle].principal)
            if actor.root and actor.root_loader is None:
                actor.root_loader = loader
            origin = "parent" if actor.root and loader == actor.root_loader else "child"
            if origin == "parent" and actor.initial_root is not None:
                admission, actor.initial_root = actor.initial_root, None
            else:
                admission = self.runtime._open_pi_scope(
                    self.session_id, origin=origin, native_guard=True
                )
            if admission.principal.launch_epoch != actor.launch_epoch:
                raise ValueError("Pi bootstrap launch changed")
            actor.installations[handle] = admission
            actor.loaders[handle] = loader
            principal = admission.principal
            self.runtime.install_pi_native_classifier(
                principal,
                lambda call_id, binding, arguments: self._classify(
                    actor, principal.scope_id, call_id, binding, arguments
                ),
            )
        if actor.loaders[handle] != loader:
            raise ValueError("Pi bootstrap loader changed")
        record = self.runtime.pi_scopes.pending(self.session_id, admission.credential)
        registry = payload.get("registry")
        ready = False
        if registry is not None:
            if not isinstance(registry, dict) or any(
                registry.get(key) != value for key, value in record.expected.items()
            ):
                raise ValueError("Pi bootstrap registry binding changed")
            atomic_write_json(admission.registry_dir / "permission_tools.json", registry)
            ready = self.runtime.acknowledge_pi_scope(self.session_id, admission.credential)
            if not ready:
                raise ValueError("Pi bootstrap registry unavailable")
        return {
            "ready": ready,
            "scope": asdict(admission.principal),
            "credential": admission.credential,
            "expected": record.expected,
            "registry_dir": str(admission.registry_dir),
            "broker_incarnation": self.incarnation,
        }

    def _classify(
        self,
        actor: PiAttestorProcess,
        scope_id: str,
        call_id: str,
        binding: NativeCallBinding,
        _arguments: Mapping[str, object],
    ) -> NativeSupport:
        support = self._proofs.get((scope_id, call_id))
        if not self.live(actor) or support is None or support.binding != binding:
            return NativeSupport(
                binding,
                NativeSupportStatus.FAILED,
                reason="Native call has no acknowledged attestation",
            )
        return support

    def _attest(
        self, admission: PiScopeAdmission, payload: dict[str, object]
    ) -> dict[str, object]:
        record = self.runtime.pi_scopes.authenticate(self.session_id, admission.credential)
        state = self.runtime.state(self.session_id)
        call_id, name, arguments = (
            payload.get("call_id"),
            payload.get("name"),
            payload.get("arguments"),
        )
        if (
            state is None
            or payload.get("revision") != state.revision
            or type(payload.get("revision")) is not int
            or not isinstance(call_id, str)
            or not call_id
            or not isinstance(name, str)
            or not name
            or not isinstance(arguments, dict)
        ):
            raise ValueError("Pi bootstrap call binding unavailable")
        principal = record.admission.principal
        binding = NativeCallBinding(
            principal.scope_id,
            principal.installation_id,
            principal.registry_generation,
            name,
            native_digest(arguments),
            state.revision,
        )
        raw = payload.get("support")
        if (
            not isinstance(raw, dict)
            or set(raw)
            - {
                "binding",
                "status",
                "action",
                "reason",
                "outside_kind",
                "inputs",
                "semantics_digest",
            }
            or raw.get("binding") != asdict(binding)
            or not isinstance(raw.get("inputs"), list)
        ):
            raise ValueError("Pi bootstrap proof binding unavailable")
        try:
            inputs = tuple(
                NativeInputSnapshot(**entry) for entry in raw["inputs"] if isinstance(entry, dict)
            )
            if len(inputs) != len(raw["inputs"]):
                raise ValueError("Malformed inputs")
            status = NativeSupportStatus(raw.get("status"))
            action = raw.get("action", "ordinary")
            if (
                action not in ("ordinary", "read", "control", "delegate")
                or not isinstance(raw.get("reason", ""), str)
                or not isinstance(raw.get("semantics_digest", ""), str)
                or (
                    raw.get("outside_kind") is not None
                    and not isinstance(raw.get("outside_kind"), str)
                )
            ):
                raise ValueError("Malformed native semantics")
            support = NativeSupport(
                binding,
                status,
                cast(Literal["ordinary", "read", "control", "delegate"], action),
                cast(str, raw.get("reason", "")),
                cast(str | None, raw.get("outside_kind")),
                inputs,
                cast(str, raw.get("semantics_digest", "")),
            )
            if status != NativeSupportStatus.FAILED:
                self.runtime._verify_native_inputs(self.session_id, support)
        except (TypeError, ValueError, OSError):
            raise ValueError("Pi bootstrap proof unavailable") from None
        canonical = (
            "pi:" + hashlib.sha256((principal.scope_id + "\0" + call_id).encode()).hexdigest()
        )
        key = principal.scope_id, canonical
        old = self._proofs.get(key)
        if old is not None and old != support:
            self.runtime.revoke_pi_scope(principal)
            raise ValueError("Pi bootstrap attestation changed")
        if key not in self._proofs and len(self._proofs) >= 32768:
            raise ValueError("Pi bootstrap proof limit reached")
        self._proofs[key] = support
        return {"attested": True, "call_id": canonical, "broker_incarnation": self.incarnation}
