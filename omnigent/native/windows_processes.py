"""Recover Windows process trees after their recorded owner has died."""

from __future__ import annotations

import json
import math
import os
import re
from contextlib import suppress
from pathlib import Path

import psutil


def process_birth(pid: int) -> float | None:
    """Return a process birth time, retaining unknown identities on errors."""
    try:
        return psutil.Process(pid).create_time()
    except psutil.Error:
        return None


def reap_record(record_path: Path) -> bool:
    """Retry a durable census without losing children when its root has exited."""
    try:
        job_path = record_path.with_name(
            record_path.name.removesuffix(".process.json") + ".job.json"
        )
        job = None
        if job_path.exists():
            job = json.loads(job_path.read_text(encoding="utf-8"))
            if not isinstance(job, dict) or not isinstance(job.get("job_name"), str):
                return False
            if re.fullmatch(r"Global\\omnigent-sdk-[0-9a-f]{32}", job["job_name"]) is None:
                return False
        data = json.loads(record_path.read_text(encoding="utf-8")) if record_path.exists() else job
        if not isinstance(data, dict):
            return False
        pid, birth = data["pid"], data["create_time"]
        if (
            type(pid) is not int
            or pid <= 1
            or pid == os.getpid()
            or type(birth) not in (int, float)
            or not math.isfinite(birth)
            or birth <= 0
        ):
            return False
        if job is not None:
            from omnigent.inner.windows_jobobject_sandbox import job_has_exited

            if (
                type(job.get("pid")) is not int
                or job["pid"] <= 1
                or job["pid"] == os.getpid()
                or type(job.get("create_time")) not in (int, float)
                or not math.isfinite(job["create_time"])
                or job["create_time"] <= 0
            ):
                return False
            # A unique launch path links redirector and interpreter identities;
            # their PIDs may differ. Missing/reused roots are never signalled.
            roots = {(pid, birth): record_path}
            roots[job["pid"], job["create_time"]] = job_path
            for (root_pid, root_birth), census_path in roots.items():
                with suppress(psutil.NoSuchProcess):
                    root = psutil.Process(root_pid)
                    if root.create_time() == root_birth:
                        if not reap_tree(root_pid, root_birth, record_path=census_path):
                            return False
            return job_has_exited(job["job_name"])
        return reap_tree(pid, birth, record_path=record_path)
    except (psutil.Error, OSError, ValueError, KeyError, TypeError):
        return False


def reap_tree(pid: int, create_time: float, *, record_path: Path | None = None) -> bool:
    """Reap only a matching root and its observed descendants; confirm exit."""
    if pid <= 1 or pid == os.getpid() or not math.isfinite(create_time) or create_time <= 0:
        return False
    try:
        data = {}
        identities = {pid: create_time}
        confirmed = False
        if record_path is not None and record_path.exists():
            data = json.loads(record_path.read_text(encoding="utf-8"))
            if data["pid"] != pid or data["create_time"] != create_time:
                return False
            if "members" in data:
                for member in data["members"]:
                    member_pid, birth = member["pid"], member["create_time"]
                    if (
                        type(member_pid) is not int
                        or type(birth) not in (int, float)
                        or member_pid <= 1
                        or not math.isfinite(birth)
                        or birth <= 0
                    ):
                        return False
                    identities[member_pid] = birth
                confirmed = True
        root = None
        with suppress(psutil.NoSuchProcess):
            candidate = psutil.Process(pid)
            if candidate.create_time() == create_time:
                root = candidate
        if root is not None:
            children = root.children(recursive=True)
            # Revalidate before adopting descendants, including on a retry.
            if not root.is_running() or root.create_time() != create_time:
                return False
            identities.update((child.pid, child.create_time()) for child in children)
            confirmed = True
        if not confirmed:
            # A root that exited before our first census leaves unclaimed children.
            # Recovering those needs launch-time ownership; never infer it from PPIDs.
            return False
        processes = []
        for member_pid, birth in reversed(identities.items()):
            if member_pid <= 1 or member_pid == os.getpid():
                return False
            with suppress(psutil.NoSuchProcess):
                process = psutil.Process(member_pid)
                if process.create_time() == birth:
                    processes.append(process)
        if record_path is not None:
            # Persist the complete census before any signal can remove the root.
            data.update(
                {
                    "pid": pid,
                    "create_time": create_time,
                    "members": [
                        {"pid": member_pid, "create_time": birth}
                        for member_pid, birth in identities.items()
                    ],
                }
            )
            temporary = record_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(data), encoding="utf-8")
            temporary.replace(record_path)
        for process in processes:
            with suppress(psutil.NoSuchProcess):
                # psutil's signal methods check creation time against PID reuse.
                process.terminate()
        _, alive = psutil.wait_procs(processes, timeout=1.5)
        for process in alive:
            with suppress(psutil.NoSuchProcess):
                process.kill()
        _, alive = psutil.wait_procs(alive, timeout=1.0)
        return not alive
    except (psutil.Error, OSError, ValueError, KeyError, TypeError):
        return False
