"""Deterministic offline execution provider (V0.2 stage 1 adapter).

The Fake provider exists so that Phase E.2 can prove the DX-internal chain
(outbox -> worker -> gateway -> lifecycle) without any external dependency. Its value
is diagnostic: if a test fails with the Fake provider, the defect is inside DX, not
inside a vendor integration.

Determinism rules:

* No clock, no randomness, no I/O. The same request always produces the same receipt
  and the same outcome.
* ``external_execution_id`` is derived from the DX execution id, never generated.
* Behaviour is selected through the constructor, so production code carries no
  test-only hook and no environment switch.
* Rejection and submit failure are returned as receipts. The one expected outcome that
  is expressed as an exception is an operational collection failure
  (``ProviderCollectionError``); genuinely unexpected failures raise RuntimeError.
"""

from __future__ import annotations

from uuid import UUID

from dxbrain.execution.contracts import (
    OUTCOME_FAILED,
    OUTCOME_SUCCEEDED,
    DispatchReceipt,
    DispatchRequest,
    ExecutionOutcome,
    ProviderCollectionError,
)

BEHAVIOR_SUCCEEDED = "succeeded"
BEHAVIOR_PRE_HANDOFF_FAILURE = "pre_handoff_failure"
BEHAVIOR_HANDOFF_REJECTED = "handoff_rejected"
BEHAVIOR_NEVER_STARTED = "never_started"
BEHAVIOR_RUNTIME_FAILED = "runtime_failed"
BEHAVIOR_COLLECT_ERROR = "collect_error"
BEHAVIOR_UNEXPECTED_ERROR = "unexpected_error"

BEHAVIORS = (
    BEHAVIOR_SUCCEEDED,
    BEHAVIOR_PRE_HANDOFF_FAILURE,
    BEHAVIOR_HANDOFF_REJECTED,
    BEHAVIOR_NEVER_STARTED,
    BEHAVIOR_RUNTIME_FAILED,
    BEHAVIOR_COLLECT_ERROR,
    BEHAVIOR_UNEXPECTED_ERROR,
)


class FakeExecutionProvider:
    """The only adapter required by V0.2 stage 1 (ADR-0010, Provider abstraction)."""

    name = "fake"

    def __init__(
        self,
        behavior: str = BEHAVIOR_SUCCEEDED,
        *,
        error_code: str | None = None,
        error_detail: str | None = None,
    ) -> None:
        if behavior not in BEHAVIORS:
            raise ValueError(f"unknown fake execution provider behavior: {behavior}")
        self._behavior = behavior
        self._error_code = error_code
        self._error_detail = error_detail

    @property
    def behavior(self) -> str:
        return self._behavior

    @staticmethod
    def external_execution_id(execution_id: UUID) -> str:
        """Derive the provider-side correlation id from the canonical DX id."""

        return f"fake-{execution_id}"

    def dispatch(self, request: DispatchRequest) -> DispatchReceipt:
        if self._behavior == BEHAVIOR_UNEXPECTED_ERROR:
            raise RuntimeError("fake provider: unexpected failure before handoff")

        if self._behavior == BEHAVIOR_PRE_HANDOFF_FAILURE:
            return DispatchReceipt(
                handoff_attempted=False,
                accepted=False,
                error_code=self._error_code or "provider_unavailable",
                error_detail=self._error_detail or "fake provider did not attempt a handoff",
            )

        if self._behavior == BEHAVIOR_HANDOFF_REJECTED:
            return DispatchReceipt(
                handoff_attempted=True,
                accepted=False,
                external_execution_id=None,
                error_code=self._error_code or "provider_reject",
                error_detail=self._error_detail or "fake provider rejected the job after handoff",
            )

        return DispatchReceipt(
            handoff_attempted=True,
            accepted=True,
            external_execution_id=self.external_execution_id(request.execution_id),
        )

    def collect(self, receipt: DispatchReceipt) -> ExecutionOutcome:
        if self._behavior == BEHAVIOR_COLLECT_ERROR:
            # An expected operational outcome (the provider could not deliver the
            # result), not an unexpected adapter bug: the Gateway maps it to
            # dispatched -> failed with error_code = provider_unavailable.
            raise ProviderCollectionError(
                "fake provider: failed to collect the execution result"
            )

        external_execution_id = receipt.external_execution_id

        if self._behavior == BEHAVIOR_NEVER_STARTED:
            return ExecutionOutcome(
                external_execution_id=external_execution_id,
                started=False,
                status=OUTCOME_FAILED,
                error_code=self._error_code or "provider_reject",
                error_detail=self._error_detail or "fake provider never started the execution",
                summary="fake execution never started",
            )

        if self._behavior == BEHAVIOR_RUNTIME_FAILED:
            return ExecutionOutcome(
                external_execution_id=external_execution_id,
                started=True,
                status=OUTCOME_FAILED,
                exit_code=1,
                summary="fake execution failed",
                result_tail="fake: runtime error",
                error_code=self._error_code or "runtime_error",
                error_detail=self._error_detail or "fake provider reported a runtime failure",
            )

        return ExecutionOutcome(
            external_execution_id=external_execution_id,
            started=True,
            status=OUTCOME_SUCCEEDED,
            exit_code=0,
            summary="fake execution completed",
            result_tail="fake: ok",
        )
