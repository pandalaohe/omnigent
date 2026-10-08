"""Deferred approvals (the server default) across a server restart.

The upstream scenarios script blocking approvals. Here the permission hook
denies at once, the card waits on the server and the model re-issues the call
once the user approves. A restart between the prompt and the answer must keep
the card answerable, deliver the approval and let the re-issued call run.
``--resilience-approvals=deferred`` runs every scenario this way.
"""

from __future__ import annotations

import time
from collections.abc import Callable

import pytest

from tests.e2e.resilience.lab.driver import SessionDriver
from tests.e2e.resilience.lab.lab import Harness, Lab
from tests.e2e.resilience.lab.report import ScenarioReport
from tests.e2e.resilience.scenarios import _contract as contract

_CASES = [
    *contract.cases([contract.APPROVAL_PENDING], [5]),
    # Only Claude asks for a workspace command, so only its tool phase defers.
    *contract.cases([contract.TOOL_RUNNING], [5], harnesses=("claude",)),
]


@pytest.mark.timeout(600)
@pytest.mark.parametrize(("harness", "phase", "outage_s"), _CASES)
def test_deferred_approval_server_restart(
    lab_factory: Callable[..., Lab], harness: Harness, phase: str, outage_s: int
) -> None:
    lab = lab_factory(approvals="deferred")
    driver = SessionDriver.create(lab, harness)
    report = ScenarioReport(
        "Deferred approval server restart",
        {"harness": harness, "phase": phase, "outage_s": outage_s},
    )
    with contract.observe(lab, driver, report) as watcher:
        entered = contract.enter(driver, phase, outage_s=outage_s)
        down_at = time.time()
        lab.restart_server(downtime_s=outage_s)
        up_at = time.time()
        contract.finish(
            report,
            lab,
            driver,
            watcher,
            entered,
            fault_start=down_at,
            fault_end=up_at,
            outage_s=outage_s,
        )
    report.require()
