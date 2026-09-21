"""Provider-neutral execution contracts for the Agent Execution Bridge (V0.2, Phase E.2A).

This module is the isolation boundary between DX-internal plumbing and external
execution providers. It imports no SQLAlchemy and no ORM model: an
``ExecutionProvider`` receives plain dataclasses and returns plain dataclasses. It
never sees a ``Session``, an ORM row, or a governance primary key.

Frozen dispatch semantics (Phase E.2A):

* ``handoff_attempted`` is the ONLY discriminator for "did DX actually attempt the
  outbound handoff". Provider acceptance must never be inferred from ``accepted``,
  and ``accepted=True`` is a contract violation unless ``handoff_attempted=True``.
* ``handoff_attempted=False``             -> ``requested -> failed``
* ``handoff_attempted=True, accepted=F``  -> ``requested -> dispatched -> failed``
* accepted + started                      -> ``requested -> dispatched -> running -> terminal``
* ``dispatched`` is a DX-side fact meaning "the handoff was attempted". It is NOT a
  statement that the provider accepted, started, or acknowledged anything.
* operational collection failure          -> ``dispatched -> failed``
  (``ProviderCollectionError``, ``error_code = provider_unavailable``)

Rejection and submit failure are expressed through ``DispatchReceipt``, never through
an exception. Only one exception class models an expected provider outcome
(``ProviderCollectionError``); every other exception is unexpected and propagates with
zero lifecycle writes.

The minimum V0.2 adapter capability set is dispatch + collect result. Status polling
and cancellation are not part of this contract (ADR-0010 defers cancellation).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol
from uuid import UUID

# Frozen by ADR-0010 and emitted by dxbrain.core.agent_executions.create
# (src/dxbrain/core/agent_executions.py:257). E.2A subscribes to this event only.
AGENT_EXECUTION_REQUESTED_EVENT = "agent_execution.requested.v1"

OUTCOME_SUCCEEDED = "succeeded"
OUTCOME_FAILED = "failed"

ExecutionOutcomeStatus = Literal["succeeded", "failed"]


class ExecutionProviderError(RuntimeError):
    """An execution provider failed in a way that is not a modelled outcome."""


class ProviderCollectionError(ExecutionProviderError):
    """An EXPECTED operational failure while collecting an execution result.

    Raised by an adapter when the provider itself failed to deliver a result for an
    already-dispatched execution (result unreachable, provider-side loss, collection
    refused). It is an operational outcome, not a programming error: the Gateway maps
    it to ``dispatched -> failed`` with ``error_code = provider_unavailable``.

    It is deliberately NOT the base class used for contract violations, so catching it
    never swallows ``ExecutionContractError`` or an unexpected adapter bug.
    """


class ExecutionContractError(ExecutionProviderError):
    """A provider returned a result that violates the dispatch contract."""


@dataclass(frozen=True)
class DispatchRequest:
    """What DX hands to a provider. Identity and governance stay on the DX side."""

    execution_id: UUID
    task_id: UUID
    capability: str
    agent_role: str
    draft_id: UUID
    draft_revision: int
    request_input: dict[str, Any]
    constraints: Mapping[str, Any] | None = None
    workspace_ref: str | None = None


@dataclass(frozen=True)
class DispatchReceipt:
    """The outcome of DX's handoff attempt.

    ``handoff_attempted`` is the frozen discriminator between "DX never got as far as
    an outbound handoff" (pre-handoff failure) and "DX attempted the handoff and the
    provider did not accept it" (post-handoff rejection). ``accepted`` must never be
    read without it.
    """

    handoff_attempted: bool
    accepted: bool
    external_execution_id: str | None = None
    error_code: str | None = None
    error_detail: str | None = None


@dataclass(frozen=True)
class ExecutionOutcome:
    """The collected result of one execution."""

    external_execution_id: str | None
    started: bool
    status: ExecutionOutcomeStatus
    exit_code: int | None = None
    summary: str | None = None
    artifacts: dict[str, Any] | None = None
    log_references: dict[str, Any] | None = None
    result_tail: str | None = None
    error_code: str | None = None
    error_detail: str | None = None


class ExecutionProvider(Protocol):
    """An agent / workforce execution provider adapter (not a model provider)."""

    name: str

    def dispatch(self, request: DispatchRequest) -> DispatchReceipt:
        """Attempt the outbound handoff and report what actually happened."""

    def collect(self, receipt: DispatchReceipt) -> ExecutionOutcome:
        """Collect the result of an accepted execution."""
