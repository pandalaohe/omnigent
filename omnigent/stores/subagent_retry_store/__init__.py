"""Transactional persistence; callers perform network effects after commit."""

from typing import Any, Protocol

from omnigent.entities.subagent_retry import (
    InboxOwnerFence,
    InputExecutionClaim,
    InputExecutionReceipt,
    RetryChain,
    RetryControl,
    RetryFinal,
    RetryInput,
    RetryInputClaim,
    RetryMutation,
    RetryOutcome,
)
from omnigent.entities.subagent_terminal import TerminalEnvelope


class SubagentRetryStore(Protocol):
    """Trusted server boundary; user permissions/routing are checked by its caller.

    Owner claims verify the persisted runner binding. The runner additionally
    proves its local inbox exists before requesting a hold. Admission receipts
    describe logical execution admission, never merely HTTP/history acceptance.
    """

    def claim_owner(
        self, parent_id: str, runner_id: str, *, expected_epoch: int
    ) -> InboxOwnerFence | None: ...

    def get_owner(self, parent_id: str) -> InboxOwnerFence | None: ...

    def get_control(self, child_id: str) -> RetryControl | None: ...

    def record_input(
        self,
        child_id: str,
        *,
        input_id: str,
        turn_id: str | None,
        expected_generation: int,
        payload: dict[str, Any],
    ) -> RetryControl | None: ...

    def open_chain(
        self,
        terminal: TerminalEnvelope,
        *,
        owner: InboxOwnerFence,
        limit: int,
        observed_at: float,
    ) -> RetryMutation | None: ...

    def get_chain(self, chain_id: str) -> RetryChain | None: ...

    def take_over(
        self,
        chain_id: str,
        *,
        owner: InboxOwnerFence,
        expected_version: int,
    ) -> RetryChain | None: ...

    def supersede(
        self,
        chain_id: str,
        *,
        owner: InboxOwnerFence,
        expected_version: int,
        replacement_input_id: str,
    ) -> RetryChain | None: ...

    def get_input(self, input_id: str) -> RetryInput | None: ...

    def prepare_attempt(
        self,
        chain_id: str,
        *,
        owner: InboxOwnerFence,
        expected_version: int,
        now: float,
    ) -> RetryInput | None: ...

    def claim_input(self, input_id: str, *, owner: InboxOwnerFence) -> RetryInputClaim | None: ...

    def record_acceptance(
        self,
        input_id: str,
        *,
        owner: InboxOwnerFence,
        turn_id: str,
    ) -> RetryInput | None: ...

    def admit_execution(
        self, child_id: str, claim: InputExecutionClaim, *, runner_id: str | None
    ) -> InputExecutionReceipt: ...

    def bind_native_turn(
        self,
        child_id: str,
        *,
        input_id: str,
        turn_id: str,
        runner_id: str,
        native_generation: int | None = None,
    ) -> InputExecutionReceipt: ...

    def resolve_execution(
        self, child_id: str, claim: InputExecutionClaim, *, runner_id: str, close: bool
    ) -> InputExecutionReceipt: ...

    def begin_control(self, child_id: str, input_id: str, *, runner_id: str | None) -> bool: ...

    def finish_control(self, child_id: str, input_id: str, *, accepted: bool) -> bool: ...

    def list_controls(self, child_id: str) -> list[RetryInput]: ...

    def retire_control(self, child_id: str, input_id: str, *, expected_version: int) -> bool: ...

    def close_input(
        self,
        input_id: str,
        *,
        owner: InboxOwnerFence,
        expected_version: int,
        reason: dict[str, Any],
    ) -> RetryInput | None: ...

    def record_terminal(
        self,
        chain_id: str,
        terminal: TerminalEnvelope,
        *,
        owner: InboxOwnerFence,
        observed_at: float,
    ) -> RetryMutation | None: ...

    def finalize(
        self,
        chain_id: str,
        *,
        owner: InboxOwnerFence,
        expected_version: int,
        outcome: RetryOutcome,
        wake_payload: dict[str, Any],
    ) -> RetryFinal | None: ...

    def get_final(self, chain_id: str) -> RetryFinal | None: ...

    def list_outbox(
        self,
        *,
        owner: InboxOwnerFence,
        limit: int = 100,
    ) -> list[RetryFinal]: ...

    def record_inbox(
        self,
        chain_id: str,
        *,
        owner: InboxOwnerFence,
        result_id: str,
    ) -> bool: ...

    def record_drain(
        self,
        chain_id: str,
        *,
        owner: InboxOwnerFence,
        result_id: str,
    ) -> bool: ...

    def transfer_parent(
        self,
        chain_id: str,
        *,
        owner: InboxOwnerFence,
        successor: InboxOwnerFence,
        expected_version: int,
    ) -> RetryChain | None: ...
