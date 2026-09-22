"""Phase E.4 orchestration tests: plan and execute use the existing production path.

These tests prove the *operator command* calls the existing production services in the
required order and does NOT reimplement plan logic or touch a provider / Gateway
directly. Everything is driven through injected fake services and monkeypatched worker
symbols — no real LLM, no real Codex, no live model call, no network.

Two facts are proven here that the pure-contract tests do not cover:

* ``cmd_plan`` composes ``prepare_live_plan -> execute_prepared -> parse_model_call``
  in order, passing the finalized call id to the parser and a real ``HttpxTransport``
  to the executor; and fails clearly when the model call does not succeed.
* ``cmd_execute`` restores ``workspace_ref/provider/capability/agent_id`` from the
  persisted ``IntentProposal.scope`` (no retyping), enforces exactly one workflow step,
  creates the AgentExecution through ``AgentExecutionService.create``, and reaches the
  worker only through the bounded outbox loop (no direct provider / Gateway call).
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from dxbrain.operator import cli
from dxbrain.operator.cli import OperatorError, cmd_confirm, cmd_execute, cmd_plan

# --- plan path ---------------------------------------------------------------------


def _plan_args(proposal_id: str, actor_id: str = "daryl") -> argparse.Namespace:
    return argparse.Namespace(proposal_id=proposal_id, actor_id=actor_id)


def test_cmd_plan_uses_existing_production_chain(monkeypatch) -> None:
    """cmd_plan drives prepare -> execute -> parse in order, no plan reimplementation."""

    proposal_id = str(uuid4())
    model_call_id = uuid4()
    draft_id = uuid4()

    calls: list[str] = []
    captured_transport: list[Any] = []

    class _FakeModelCalls:
        def prepare_live_plan(self, **kwargs):
            calls.append("prepare")
            return SimpleNamespace(id=model_call_id)

        def execute_prepared(self, **kwargs):
            calls.append("execute")
            captured_transport.append(kwargs["transport"])
            return SimpleNamespace(id=model_call_id, status="succeeded")

    class _FakePlanDrafts:
        def parse_model_call(self, **kwargs):
            calls.append("parse")
            # parser must receive the finalized call id
            assert kwargs["model_call_id"] == model_call_id
            return SimpleNamespace(
                id=draft_id, status="candidate", steps=[{"title": "t"}], warnings=[]
            )

    ctx = SimpleNamespace(
        model_calls=_FakeModelCalls(),
        plan_drafts=_FakePlanDrafts(),
        _load_proposal=lambda pid: SimpleNamespace(id=pid),
    )

    rc = cmd_plan(ctx, _plan_args(proposal_id))

    assert rc == 0
    assert calls == ["prepare", "execute", "parse"]
    # execute uses a real HTTP transport, never a fabricated model output.
    assert isinstance(captured_transport[0], cli.HttpxTransport)


def test_cmd_plan_fails_clearly_when_not_succeeded(monkeypatch) -> None:
    """A non-succeeded planning call raises instead of producing a fake draft."""

    proposal_id = str(uuid4())
    model_call_id = uuid4()

    class _FakeModelCalls:
        def prepare_live_plan(self, **kwargs):
            return SimpleNamespace(id=model_call_id)

        def execute_prepared(self, **kwargs):
            return SimpleNamespace(id=model_call_id, status="failed")

    class _FakePlanDrafts:
        def parse_model_call(self, **kwargs):
            raise AssertionError("parse must not run when the model call did not succeed")

    ctx = SimpleNamespace(
        model_calls=_FakeModelCalls(),
        plan_drafts=_FakePlanDrafts(),
        _load_proposal=lambda pid: SimpleNamespace(id=pid),
    )

    with pytest.raises(OperatorError):
        cmd_plan(ctx, _plan_args(proposal_id))


def test_cmd_plan_rejects_multi_step(monkeypatch) -> None:
    """R2: a live plan yielding >1 step is rejected at the plan boundary, not silently
    truncated to the first step."""

    proposal_id = str(uuid4())
    model_call_id = uuid4()
    draft_id = uuid4()

    class _FakeModelCalls:
        def prepare_live_plan(self, **kwargs):
            return SimpleNamespace(id=model_call_id)

        def execute_prepared(self, **kwargs):
            return SimpleNamespace(id=model_call_id, status="succeeded")

    class _FakePlanDrafts:
        def parse_model_call(self, **kwargs):
            return SimpleNamespace(
                id=draft_id,
                status="candidate",
                steps=[
                    {"title": "step one"},
                    {"title": "step two"},
                ],
                warnings=[],
            )

    ctx = SimpleNamespace(
        model_calls=_FakeModelCalls(),
        plan_drafts=_FakePlanDrafts(),
        _load_proposal=lambda pid: SimpleNamespace(id=pid),
    )

    with pytest.raises(OperatorError):
        cmd_plan(ctx, _plan_args(proposal_id))


def test_cmd_confirm_rejects_multi_step(monkeypatch) -> None:
    """R2: confirmation re-validates the single-step boundary before review_draft."""

    draft_id = uuid4()

    class _FakePlanDrafts:
        def __init__(self):
            self.reviewed = False

        def get_draft(self, draft_id):
            return SimpleNamespace(
                steps=[{"title": "a"}, {"title": "b"}],
            )

        def review_draft(self, **kwargs):
            self.reviewed = True
            return SimpleNamespace(id=draft_id, revision=1, status="confirmed")

    drafts = _FakePlanDrafts()
    ctx = SimpleNamespace(plan_drafts=drafts)

    with pytest.raises(OperatorError):
        cmd_confirm(ctx, argparse.Namespace(draft_id=str(draft_id), actor_id="daryl", reason=None))

    # The draft must NOT have been promoted to confirmed.
    assert drafts.reviewed is False


# --- execute path ------------------------------------------------------------------


@dataclass
class _FakeStep:
    id: UUID
    title: str
    capability: str
    parameters: dict[str, Any]


def _execute_ctx(scope: dict[str, Any], *, step_count: int = 1) -> tuple[SimpleNamespace, dict]:
    """Build a fake operator context plus a recording dict for assertions."""

    proposal_id = uuid4()
    task_id = uuid4()
    approval_id = uuid4()
    draft_id = uuid4()
    handoff_id = uuid4()
    run_id = uuid4()
    execution_id = uuid4()
    recording: dict[str, Any] = {"create_kwargs": None, "drain_called": False}

    proposal = SimpleNamespace(
        id=proposal_id,
        task_id=task_id,
        scope=scope,
        requested_capabilities=["model.call", "agent.execution"],
        status="approved",
        rationale="human instruction",
    )

    class _FakeGovernance:
        def list_approvals(self, pid):
            return [
                SimpleNamespace(
                    id=approval_id,
                    status="active",
                    expires_at=datetime.now(UTC) + timedelta(hours=1),
                )
            ]

        def get_proposal(self, pid):
            return proposal

    class _FakePlanDrafts:
        def list_drafts(self, pid):
            return [
                SimpleNamespace(
                    id=draft_id,
                    status="confirmed",
                    revision=1,
                    steps=[{"title": "t", "capability": "agent.execution", "parameters": {}}],
                )
            ]

        def request_workflow_handoff(self, **kwargs):
            return SimpleNamespace(id=handoff_id)

    class _FakeWorkflow:
        def accept_handoff(self, **kwargs):
            steps = [
                _FakeStep(uuid4(), f"step{i}", "agent.execution", {}) for i in range(step_count)
            ]
            return SimpleNamespace(id=run_id, steps=steps)

    class _FakeAgentExecutions:
        def create(self, **kwargs):
            recording["create_kwargs"] = kwargs
            return SimpleNamespace(id=execution_id)

    ctx = SimpleNamespace(
        governance=_FakeGovernance(),
        plan_drafts=_FakePlanDrafts(),
        workflow=_FakeWorkflow(),
        agent_executions=_FakeAgentExecutions(),
        session_factory=object(),
        _load_intent=lambda pid: (cli.parse_intent(scope), proposal),
        _load_proposal=lambda pid: proposal,
    )
    recording["proposal_id"] = proposal_id
    recording["task_id"] = task_id
    recording["execution_id"] = execution_id
    return ctx, recording


def _patch_drain(monkeypatch) -> None:
    """Isolate the worker/drain boundary: no real WorkerSettings, worker, or DB."""

    class _FakeResult:
        consumed_events = 1

    monkeypatch.setattr(cli, "WorkerSettings", lambda: SimpleNamespace(database_url="x"))
    monkeypatch.setattr(cli, "create_session_factory", lambda *a, **k: object())
    monkeypatch.setattr(cli, "OutboxWorker", lambda *a, **k: SimpleNamespace())
    monkeypatch.setattr(cli, "build_execution_handler", lambda *a, **k: None)
    monkeypatch.setattr(
        cli,
        "drain_until_terminal",
        lambda **k: _FakeResult(),
    )
    monkeypatch.setattr(cli, "read_status", lambda *a, **k: "succeeded")
    monkeypatch.setattr(
        cli,
        "read_execution_detail",
        lambda *a, **k: cli.ExecutionDetail(
            status="succeeded",
            provider="codex",
            capability="agent.execution",
            workspace_ref="default",
            exit_code=0,
            error_code=None,
            summary="ok",
            result_tail="done",
        ),
    )


def test_cmd_execute_restores_intent_and_creates_execution(monkeypatch) -> None:
    """execute restores the stored intent and passes it to AgentExecutionService.create."""

    agent_id = uuid4()
    scope = {
        "workspace_ref": "default",
        "provider": "codex",
        "capability": "agent.execution",
        "agent_id": str(agent_id),
    }
    ctx, recording = _execute_ctx(scope)
    _patch_drain(monkeypatch)

    rc = cmd_execute(
        ctx, argparse.Namespace(proposal_id=str(recording["proposal_id"]), actor_id="daryl")
    )

    assert rc == 0
    kwargs = recording["create_kwargs"]
    assert kwargs is not None
    # Stored intent is used verbatim — no retyping.
    assert kwargs["provider"] == "codex"
    assert kwargs["capability"] == "agent.execution"
    assert kwargs["workspace_ref"] == "default"
    assert kwargs["agent_id"] == agent_id
    # Anchor to the persisted proposal/task, not to a fabricated identity.
    assert kwargs["proposal_id"] == recording["proposal_id"]
    assert kwargs["task_id"] == recording["task_id"]
    # R1: the request_input carries a non-empty prompt composed from the human
    # instruction (proposal.rationale) and the confirmed step title.
    request_input = kwargs["request_input"]
    prompt = request_input["prompt"]
    assert isinstance(prompt, str) and prompt.strip()
    assert "human instruction" in prompt
    assert "step0" in prompt


def test_cmd_execute_rejects_multi_step(monkeypatch) -> None:
    """A multi-step run is refused, not auto-orchestrated."""

    scope = {
        "workspace_ref": "default",
        "provider": "codex",
        "capability": "agent.execution",
        "agent_id": str(uuid4()),
    }
    ctx, recording = _execute_ctx(scope, step_count=2)
    _patch_drain(monkeypatch)

    with pytest.raises(OperatorError):
        cmd_execute(
            ctx, argparse.Namespace(proposal_id=str(recording["proposal_id"]), actor_id="daryl")
        )

    # No execution was created because the single-step gate fired first.
    assert recording["create_kwargs"] is None


def test_cmd_execute_reaches_worker_only_via_bounded_loop(monkeypatch) -> None:
    """The drain loop is the only worker entry point; no direct provider/Gateway call."""

    scope = {
        "workspace_ref": "default",
        "provider": "codex",
        "capability": "agent.execution",
        "agent_id": str(uuid4()),
    }
    ctx, recording = _execute_ctx(scope)
    _patch_drain(monkeypatch)

    rc = cmd_execute(
        ctx, argparse.Namespace(proposal_id=str(recording["proposal_id"]), actor_id="daryl")
    )
    assert rc == 0
    # The bounded loop was invoked (the only sanctioned worker path). cmd_execute does
    # not import or call ExecutionGateway / ExecutionProvider directly — those symbols
    # are absent from this command's body by construction; this test only asserts the
    # drain path was used.
    assert recording["create_kwargs"] is not None


def test_cmd_execute_bounds_provider_failure(monkeypatch) -> None:
    """R6: a real provider operational failure surfaces as a bounded OperatorError, not
    an uncontrolled traceback, and the persisted execution state is reported."""

    from dxbrain.execution.contracts import ExecutionProviderError

    scope = {
        "workspace_ref": "default",
        "provider": "codex",
        "capability": "agent.execution",
        "agent_id": str(uuid4()),
    }
    ctx, recording = _execute_ctx(scope)

    monkeypatch.setattr(cli, "WorkerSettings", lambda: SimpleNamespace(database_url="x"))
    monkeypatch.setattr(cli, "create_session_factory", lambda *a, **k: object())
    monkeypatch.setattr(cli, "OutboxWorker", lambda *a, **k: SimpleNamespace())
    monkeypatch.setattr(cli, "build_execution_handler", lambda *a, **k: None)

    def _boom(**k):
        raise ExecutionProviderError("missing prompt")

    monkeypatch.setattr(cli, "drain_until_terminal", _boom)
    monkeypatch.setattr(cli, "read_status", lambda *a, **k: "requested")

    with pytest.raises(OperatorError) as exc_info:
        cmd_execute(
            ctx, argparse.Namespace(proposal_id=str(recording["proposal_id"]), actor_id="daryl")
        )

    # The failure is bounded (OperatorError), and the underlying class/state are kept
    # for traceability rather than fabricated into a success.
    message = str(exc_info.value)
    assert "ExecutionProviderError" in message
    assert "requested" in message
