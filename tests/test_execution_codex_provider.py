"""Phase E.3B tests: the real Codex CLI execution provider (no database, no real Codex).

Scope discipline: this module never touches the database, the OutboxWorker, the worker
CLI, the Gateway or the E.1 service. It exercises ``CodexCLIProvider`` directly against a
real workspace directory and real *substitute* child processes: ``subprocess.Popen`` is
replaced by a factory that spawns a controllable Python child, so every termination,
timeout and cleanup path is genuinely executed rather than simulated in-process. No Codex
prompt is ever run and no Codex credential is ever read.

What must be proven here:

A. the argv is exactly the approved E.3B shape and the prompt never enters it;
B. the workspace is resolved again inside ``dispatch()``, immediately before ``Popen``,
   and an unknown or vanished workspace produces zero spawn;
C. ``dispatch()`` is spawn-only: a Popen failure before the child exists is a pre-handoff
   failure, whereas a child that exists keeps ``handoff_attempted=True`` even when the
   stdin handoff then fails (and leaks nothing);
D. ``collect()`` owns wait/timeout/terminate/kill/reap/handle-cleanup on every terminal
   path, and a missing handle uses the existing collection exception type;
E. the module's own source keeps the credential and permission posture it promises.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from dxbrain.execution import codex_cli
from dxbrain.execution.codex_cli import (
    PROVIDER_DUPLICATE_DISPATCH,
    PROVIDER_STDIN_HANDOFF_FAILED,
    PROVIDER_TIMEOUT,
    PROVIDER_UNAVAILABLE,
    RESULT_TAIL_LIMIT,
    RUNTIME_ERROR,
    WORKSPACE_DENIED,
    CodexCLIProvider,
    build_argv,
    validate_mcp_server_name,
    validate_native_executable,
)
from dxbrain.execution.contracts import (
    OUTCOME_FAILED,
    OUTCOME_SUCCEEDED,
    DispatchReceipt,
    DispatchRequest,
    ExecutionProviderError,
    ProviderCollectionError,
)
from dxbrain.execution.workspace import WorkspaceResolver

_MODULE_PATH = Path(codex_cli.__file__)

WORKSPACE_KEY = "ws"
PROMPT = "report the first line of README.md"

# Child programs used in place of `codex exec`. Every one of them READS STDIN first, so the
# provider's stdin handoff is a genuine write into a live consumer rather than a race
# against a child that has already exited.
_CHILD_REPORTS_CWD = (
    "import os, sys; sys.stdin.buffer.read(); "
    "sys.stdout.write('cwd=' + os.getcwd()); sys.stdout.flush()"
)
_CHILD_SILENT = "import sys; sys.stdin.buffer.read()"
_CHILD_EXITS_THREE = "import sys; sys.stdin.buffer.read(); sys.exit(3)"
_CHILD_SLEEPS = "import sys, time; sys.stdin.buffer.read(); time.sleep(30)"
_CHILD_SLEEPS_WITHOUT_READING = "import time; time.sleep(30)"
_CHILD_WRITES_A_MEGABYTE = (
    "import sys; sys.stdin.buffer.read(); "
    "sys.stdout.buffer.write(b'x' * (1024 * 1024)); sys.stdout.flush()"
)


# --------------------------------------------------------------------------------------
# local helpers
# --------------------------------------------------------------------------------------


def _workspace(root: Path) -> Path:
    workspace = root / "ws"
    workspace.mkdir()
    return workspace


def _resolver(workspace: Path) -> WorkspaceResolver:
    return WorkspaceResolver({WORKSPACE_KEY: str(workspace)})


def _executable(root: Path) -> Path:
    """A configured, validated and never-executed stand-in for the Codex binary."""

    binary_dir = root / "bin"
    binary_dir.mkdir(exist_ok=True)
    binary = binary_dir / "codex.exe"
    binary.write_bytes(b"MZ")
    return binary


def _request(*, workspace_ref: str | None = WORKSPACE_KEY, prompt: Any = PROMPT) -> DispatchRequest:
    return DispatchRequest(
        execution_id=uuid4(),
        task_id=uuid4(),
        capability="agent.execution",
        agent_role="executor",
        draft_id=uuid4(),
        draft_revision=1,
        request_input={"prompt": prompt},
        constraints=None,
        workspace_ref=workspace_ref,
    )


def _patch_popen(
    monkeypatch: pytest.MonkeyPatch,
    script: str,
    *,
    preclose_stdin: bool = False,
    neutralize_terminate: bool = False,
    must_not_spawn: bool = False,
    probe: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    """Replace ``subprocess.Popen`` with a factory that spawns a controllable child.

    The replacement ignores the provider's argv (which names the Codex binary) and runs a
    Python child instead, while still honouring the ``cwd`` the provider asked for. That
    keeps the OS-level working directory under test while removing any dependency on a real
    Codex installation.
    """

    observed: dict[str, Any] = {"spawns": 0}
    real_popen = subprocess.Popen

    def _factory(argv: list[str], **kwargs: Any) -> subprocess.Popen[bytes]:
        observed["spawns"] += 1
        observed["argv"] = list(argv)
        observed["cwd"] = kwargs.get("cwd")
        observed["probe"] = probe() if probe is not None else None
        if must_not_spawn:
            raise AssertionError("the provider must not spawn a child on this path")

        process = real_popen(
            [sys.executable, "-c", script],
            cwd=kwargs.get("cwd"),
            stdin=kwargs.get("stdin"),
            stdout=kwargs.get("stdout"),
            stderr=kwargs.get("stderr"),
        )
        if preclose_stdin and process.stdin is not None:
            # Deterministic post-spawn failure: the provider's write hits a closed stream.
            process.stdin.close()
        if neutralize_terminate:
            # Windows `terminate()` is unconditional, so the only way to reach the kill
            # escalation is to make termination a no-op on this child.
            process.terminate = lambda: None  # type: ignore[method-assign]
            real_kill = process.kill

            def _recorded_kill() -> None:
                observed["killed"] = True
                real_kill()

            process.kill = _recorded_kill  # type: ignore[method-assign]
        observed["process"] = process
        return process

    monkeypatch.setattr(codex_cli.subprocess, "Popen", _factory)
    return observed


def _provider(root: Path, workspace: Path, **overrides: Any) -> CodexCLIProvider:
    kwargs: dict[str, Any] = {
        "executable": str(_executable(root)),
        "workspace_resolver": _resolver(workspace),
    }
    kwargs.update(overrides)
    return CodexCLIProvider(**kwargs)


def _module_executable_source() -> str:
    """The provider's code with every docstring removed.

    The static guard-rails below are about what the module *does*, so they must not be
    tripped by its own prose -- which legitimately names the very things the code must not
    contain (``sqlalchemy``, ``CODEX_HOME``).
    """

    tree = ast.parse(_MODULE_PATH.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(body, list) or not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            node.body = body[1:]
    return ast.unparse(tree)


# --------------------------------------------------------------------------------------
# A. argv contract
# --------------------------------------------------------------------------------------


def test_argv_is_exact_and_the_prompt_never_enters_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workspace = _workspace(tmp_path)
    binary = _executable(tmp_path)
    observed = _patch_popen(monkeypatch, _CHILD_REPORTS_CWD)

    provider = CodexCLIProvider(
        executable=str(binary), workspace_resolver=_resolver(workspace)
    )
    receipt = provider.dispatch(_request())

    assert provider.name == "codex"
    assert receipt.handoff_attempted is True
    assert receipt.accepted is True
    assert observed["argv"] == [
        str(binary),
        "--ask-for-approval",
        "never",
        "exec",
        "--sandbox",
        "read-only",
        "--color",
        "never",
        "--cd",
        str(workspace.resolve()),
        "-",
    ]
    assert not any(PROMPT in argument for argument in observed["argv"])
    assert observed["cwd"] == str(workspace.resolve())


def test_build_argv_is_a_pure_function() -> None:
    workspace = Path("C:/ws")

    assert build_argv("C:/bin/codex.exe", workspace) == [
        "C:/bin/codex.exe",
        "--ask-for-approval",
        "never",
        "exec",
        "--sandbox",
        "read-only",
        "--color",
        "never",
        "--cd",
        str(workspace),
        "-",
    ]


def test_build_argv_with_no_disabled_mcp_servers_matches_the_exact_shape() -> None:
    """Requirement A: the empty default yields the pre-isolation argv byte-for-byte."""

    workspace = Path("C:/ws")
    assert build_argv("C:/bin/codex.exe", workspace) == build_argv(
        "C:/bin/codex.exe", workspace, ()
    )


def test_build_argv_emits_one_c_override_per_disabled_mcp_server_before_exec() -> None:
    """Requirement B: one name -> one ``-c mcp_servers.<name>.enabled=false`` pair."""

    workspace = Path("C:/ws")
    argv = build_argv("C:/bin/codex.exe", workspace, ("openaiDeveloperDocs",))

    assert argv == [
        "C:/bin/codex.exe",
        "-c",
        "mcp_servers.openaiDeveloperDocs.enabled=false",
        "--ask-for-approval",
        "never",
        "exec",
        "--sandbox",
        "read-only",
        "--color",
        "never",
        "--cd",
        str(workspace),
        "-",
    ]


def test_build_argv_preserves_deterministic_order_for_multiple_servers() -> None:
    """Requirement C: multiple names produce repeated ``-c`` pairs in input order."""

    workspace = Path("C:/ws")
    argv = build_argv("C:/bin/codex.exe", workspace, ("alpha", "beta_1", "gamma-2"))

    override_pairs = [
        argv[i : i + 2] for i in range(len(argv)) if argv[i] == "-c"
    ]
    assert override_pairs == [
        ["-c", "mcp_servers.alpha.enabled=false"],
        ["-c", "mcp_servers.beta_1.enabled=false"],
        ["-c", "mcp_servers.gamma-2.enabled=false"],
    ]
    # The prompt sentinel ``-`` must still be the final argument.
    assert argv[-1] == "-"
    assert argv.count("-c") == 3


@pytest.mark.parametrize(
    "bad_name",
    [
        "a.b",           # dot would alter the dotted config path
        "a=b",           # equals would smuggle a key=value fragment
        "a b",           # whitespace
        "a;b",           # shell metacharacter
        "a/b",           # path separator
        "a\\b",          # path separator
        "a\"b",          # quote
        "--flag",        # CLI fragment
        "",              # empty
    ],
)
def test_validate_mcp_server_name_rejects_injection_forms(bad_name: str) -> None:
    """Requirement E: only ``[A-Za-z0-9_-]`` is accepted; injection shapes are refused."""

    with pytest.raises(RuntimeError):
        validate_mcp_server_name(bad_name)


def test_validate_mcp_server_name_accepts_the_observed_safe_form() -> None:
    assert validate_mcp_server_name("openaiDeveloperDocs") == "openaiDeveloperDocs"
    assert validate_mcp_server_name("node_repl") == "node_repl"
    assert validate_mcp_server_name("cua_repl") == "cua_repl"


def test_provider_rejects_an_invalid_disabled_mcp_name_before_spawn(tmp_path: Path) -> None:
    """Requirement E: a bad configured name fails fast at construction, no Popen."""

    binary = _executable(tmp_path)
    with pytest.raises(RuntimeError):
        CodexCLIProvider(
            executable=str(binary),
            workspace_resolver=_resolver(tmp_path),
            disabled_mcp_servers=("a.b",),
        )


def test_provider_module_has_no_shell_bypass_or_credential_surface() -> None:
    """Static guard-rail: no shell, no dangerous bypass, no credential handling."""

    source = _module_executable_source()

    for forbidden in (
        "shell=True",
        "dangerously-bypass",
        "cmd.exe",
        "powershell",
        "auth.json",
        "CODEX_HOME",
        "login",
    ):
        assert forbidden not in source


def test_provider_module_imports_no_sqlalchemy_and_no_dx_core() -> None:
    """The provider boundary stays a plain-dataclass boundary (mirrors E.2A's rule)."""

    source = _module_executable_source()

    assert "sqlalchemy" not in source
    assert "dxbrain.core" not in source
    assert "AgentExecution" not in source


# --------------------------------------------------------------------------------------
# executable validation (fail fast during construction)
# --------------------------------------------------------------------------------------


def test_valid_native_executable_is_accepted(tmp_path: Path) -> None:
    binary = _executable(tmp_path)

    assert validate_native_executable(str(binary)) == str(binary)


@pytest.mark.parametrize(
    "case",
    [
        pytest.param("missing-config", id="absent"),
        pytest.param("blank", id="blank"),
        pytest.param("relative", id="relative-path"),
        pytest.param("shim", id="npm-cmd-shim"),
        pytest.param("not-a-file", id="missing-file"),
        pytest.param("directory", id="directory"),
        pytest.param("not-mz", id="not-a-native-binary"),
    ],
)
def test_unusable_executable_configuration_fails_fast(tmp_path: Path, case: str) -> None:
    binary = _executable(tmp_path)
    if case == "missing-config":
        configured: str | None = None
    elif case == "blank":
        configured = "   "
    elif case == "relative":
        configured = "codex.exe"
    elif case == "shim":
        configured = str(tmp_path / "codex.cmd")
    elif case == "not-a-file":
        configured = str(tmp_path / "absent.exe")
    elif case == "directory":
        configured = str(tmp_path)
    else:
        broken = tmp_path / "not-mz.exe"
        broken.write_bytes(b"#!/bin/sh\n")
        configured = str(broken)

    with pytest.raises(RuntimeError):
        CodexCLIProvider(executable=configured, workspace_resolver=_resolver(binary.parent))

    # Construction never spawns anything.
    assert not (tmp_path / "absent.exe").exists()


# --------------------------------------------------------------------------------------
# B. workspace: resolved again immediately before Popen
# --------------------------------------------------------------------------------------


def test_dispatch_resolves_the_workspace_again_immediately_before_popen(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workspace = _workspace(tmp_path)
    resolver = _resolver(workspace)
    calls: list[str | None] = []
    real_resolve = resolver.resolve

    def _recording_resolve(workspace_ref: str | None) -> Path:
        calls.append(workspace_ref)
        return real_resolve(workspace_ref)

    resolver.resolve = _recording_resolve  # type: ignore[method-assign]
    observed = _patch_popen(
        monkeypatch, _CHILD_REPORTS_CWD, probe=lambda: list(calls)
    )

    provider = CodexCLIProvider(
        executable=str(_executable(tmp_path)), workspace_resolver=resolver
    )
    assert calls == []

    provider.dispatch(_request())

    # The key was resolved inside dispatch(), and the resolutions were already complete by
    # the time Popen was reached.
    assert observed["probe"] == [WORKSPACE_KEY]
    assert calls == [WORKSPACE_KEY]


def test_unknown_workspace_ref_is_refused_with_zero_spawn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workspace = _workspace(tmp_path)
    observed = _patch_popen(monkeypatch, _CHILD_REPORTS_CWD, must_not_spawn=True)
    provider = _provider(tmp_path, workspace)

    receipt = provider.dispatch(_request(workspace_ref="not-configured"))

    assert receipt.handoff_attempted is False
    assert receipt.accepted is False
    assert receipt.error_code == WORKSPACE_DENIED
    assert receipt.error_detail
    assert observed["spawns"] == 0
    assert provider.pending_handles == ()


def test_workspace_that_disappeared_after_configuration_is_refused_with_zero_spawn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The second resolution is the last-moment re-validation, not a repeat of the first."""

    workspace = _workspace(tmp_path)
    provider = _provider(tmp_path, workspace)
    os.rmdir(workspace)
    observed = _patch_popen(monkeypatch, _CHILD_REPORTS_CWD, must_not_spawn=True)

    receipt = provider.dispatch(_request())

    assert receipt.handoff_attempted is False
    assert receipt.error_code == WORKSPACE_DENIED
    assert observed["spawns"] == 0


# --------------------------------------------------------------------------------------
# C. dispatch is spawn-only
# --------------------------------------------------------------------------------------


def test_popen_failure_before_the_child_exists_is_a_pre_handoff_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workspace = _workspace(tmp_path)
    events: list[str] = []

    def _failing_popen(argv: list[str], **kwargs: Any) -> subprocess.Popen[bytes]:
        events.append("popen")
        raise OSError(2, "The system cannot find the file specified")

    monkeypatch.setattr(codex_cli.subprocess, "Popen", _failing_popen)
    provider = _provider(tmp_path, workspace)

    receipt = provider.dispatch(_request())

    assert events == ["popen"]
    assert receipt.handoff_attempted is False
    assert receipt.accepted is False
    assert receipt.error_code == PROVIDER_UNAVAILABLE
    assert receipt.error_detail
    assert provider.pending_handles == ()

    # Nothing to collect: the existing collection exception type carries it.
    with pytest.raises(ProviderCollectionError):
        provider.collect(receipt)


def test_missing_prompt_raises_before_any_spawn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workspace = _workspace(tmp_path)
    observed = _patch_popen(monkeypatch, _CHILD_REPORTS_CWD, must_not_spawn=True)
    provider = _provider(tmp_path, workspace)

    with pytest.raises(ExecutionProviderError):
        provider.dispatch(_request(prompt=None))

    assert observed["spawns"] == 0
    assert provider.pending_handles == ()


def test_successful_spawn_stores_the_handle_and_reports_handoff_attempted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workspace = _workspace(tmp_path)
    _patch_popen(monkeypatch, _CHILD_REPORTS_CWD)
    provider = _provider(tmp_path, workspace)
    request = _request()

    receipt = provider.dispatch(request)

    expected_id = f"codex-{request.execution_id}"
    assert receipt.handoff_attempted is True
    assert receipt.accepted is True
    assert receipt.external_execution_id == expected_id
    assert receipt.error_code is None
    assert provider.pending_handles == (expected_id,)
    assert provider.collect(receipt).status == OUTCOME_SUCCEEDED


def test_duplicate_dispatch_is_refused_by_receipt_without_a_second_spawn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workspace = _workspace(tmp_path)
    observed = _patch_popen(monkeypatch, _CHILD_REPORTS_CWD)
    provider = _provider(tmp_path, workspace)
    request = _request()

    first = provider.dispatch(request)
    second = provider.dispatch(request)

    assert observed["spawns"] == 1
    assert first.accepted is True
    assert second.handoff_attempted is False
    assert second.accepted is False
    assert second.error_code == PROVIDER_DUPLICATE_DISPATCH
    assert len(provider.pending_handles) == 1

    provider.collect(first)


def test_post_spawn_stdin_handoff_failure_keeps_handoff_attempted_and_leaks_no_child(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Once the child exists, `handoff_attempted` stays True and nothing is leaked."""

    workspace = _workspace(tmp_path)
    observed = _patch_popen(
        monkeypatch, _CHILD_SLEEPS_WITHOUT_READING, preclose_stdin=True
    )
    provider = _provider(tmp_path, workspace)
    request = _request()

    receipt = provider.dispatch(request)

    assert observed["spawns"] == 1
    assert receipt.handoff_attempted is True
    assert receipt.accepted is False
    assert receipt.error_code == PROVIDER_STDIN_HANDOFF_FAILED
    assert receipt.error_detail
    assert receipt.external_execution_id == f"codex-{request.execution_id}"
    # The child was terminated and reaped, and its handle was forgotten.
    assert observed["process"].poll() is not None
    assert provider.pending_handles == ()
    with pytest.raises(ProviderCollectionError):
        provider.collect(receipt)


# --------------------------------------------------------------------------------------
# D. collect: wait / timeout / terminate / kill / reap / cleanup
# --------------------------------------------------------------------------------------


def test_success_outcome_runs_in_the_resolved_workspace_and_forgets_the_handle(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workspace = _workspace(tmp_path)
    observed = _patch_popen(monkeypatch, _CHILD_REPORTS_CWD)
    provider = _provider(tmp_path, workspace)

    receipt = provider.dispatch(_request())
    outcome = provider.collect(receipt)

    reported_cwd = (outcome.result_tail or "").removeprefix("cwd=")
    assert outcome.status == OUTCOME_SUCCEEDED
    assert outcome.started is True
    assert outcome.exit_code == 0
    assert outcome.external_execution_id == receipt.external_execution_id
    assert outcome.error_code is None
    # The OS-level cwd really was the resolved workspace, not the provider's own cwd.
    assert os.path.normcase(os.path.realpath(reported_cwd)) == os.path.normcase(
        str(workspace.resolve())
    )
    assert observed["process"].poll() is not None
    assert provider.pending_handles == ()


def test_silent_child_yields_no_result_tail(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workspace = _workspace(tmp_path)
    _patch_popen(monkeypatch, _CHILD_SILENT)
    provider = _provider(tmp_path, workspace)

    outcome = provider.collect(provider.dispatch(_request()))

    assert outcome.status == OUTCOME_SUCCEEDED
    assert outcome.result_tail is None
    assert provider.pending_handles == ()


def test_non_zero_exit_is_a_runtime_error_carrying_the_provider_exit_code(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workspace = _workspace(tmp_path)
    _patch_popen(monkeypatch, _CHILD_EXITS_THREE)
    provider = _provider(tmp_path, workspace)

    outcome = provider.collect(provider.dispatch(_request()))

    assert outcome.status == OUTCOME_FAILED
    assert outcome.started is True
    assert outcome.exit_code == 3
    assert outcome.error_code == RUNTIME_ERROR
    assert provider.pending_handles == ()


def test_timeout_terminates_the_child_and_reports_provider_timeout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workspace = _workspace(tmp_path)
    observed = _patch_popen(monkeypatch, _CHILD_SLEEPS)
    provider = _provider(tmp_path, workspace, timeout_seconds=0.3)

    outcome = provider.collect(provider.dispatch(_request()))

    assert outcome.status == OUTCOME_FAILED
    assert outcome.started is True
    # A DX-imposed termination must never be reported as a provider exit code.
    assert outcome.exit_code is None
    assert outcome.error_code == PROVIDER_TIMEOUT
    assert "timeout_seconds=0.3" in (outcome.error_detail or "")
    assert observed["process"].poll() is not None
    assert provider.pending_handles == ()


def test_ineffective_terminate_escalates_to_kill(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workspace = _workspace(tmp_path)
    observed = _patch_popen(
        monkeypatch, _CHILD_SLEEPS, neutralize_terminate=True
    )
    provider = _provider(
        tmp_path, workspace, timeout_seconds=0.3, terminate_grace_seconds=0.3
    )

    outcome = provider.collect(provider.dispatch(_request()))

    assert observed["killed"] is True
    assert outcome.status == OUTCOME_FAILED
    assert outcome.error_code == PROVIDER_TIMEOUT
    assert "killed" in (outcome.error_detail or "")
    assert observed["process"].poll() is not None
    assert provider.pending_handles == ()


@pytest.mark.parametrize(
    "external_execution_id",
    [
        pytest.param("codex-00000000-0000-0000-0000-000000000000", id="unknown-id"),
        pytest.param(None, id="none"),
    ],
)
def test_collect_without_a_handle_raises_the_collection_error(
    tmp_path: Path, external_execution_id: str | None
) -> None:
    workspace = _workspace(tmp_path)
    provider = _provider(tmp_path, workspace)

    with pytest.raises(ProviderCollectionError):
        provider.collect(
            DispatchReceipt(
                handoff_attempted=True,
                accepted=True,
                external_execution_id=external_execution_id,
            )
        )


def test_large_output_is_collected_after_the_manual_stdin_handoff(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Proof for the stdin/communicate interaction and for the bounded output contract.

    `dispatch()` closes stdin by hand and clears `process.stdin`; `collect()` then calls
    `communicate()` on the same Popen. If the closed stream were still registered, CPython
    would flush a closed file object and raise instead of returning the output. A megabyte
    of stdout also overflows the pipe buffer, so this only passes if `collect()` drains the
    pipes while waiting rather than waiting before reading.
    """

    workspace = _workspace(tmp_path)
    _patch_popen(monkeypatch, _CHILD_WRITES_A_MEGABYTE)
    provider = _provider(tmp_path, workspace, timeout_seconds=30)

    outcome = provider.collect(provider.dispatch(_request()))

    assert outcome.status == OUTCOME_SUCCEEDED
    assert outcome.exit_code == 0
    assert outcome.result_tail is not None
    assert len(outcome.result_tail) == RESULT_TAIL_LIMIT
    assert provider.pending_handles == ()


def test_external_execution_id_is_derived_from_the_dx_execution_id() -> None:
    execution_id = UUID("11111111-2222-3333-4444-555555555555")

    assert CodexCLIProvider.external_execution_id(execution_id) == f"codex-{execution_id}"
    assert CodexCLIProvider.external_execution_id(execution_id).startswith("codex-")
