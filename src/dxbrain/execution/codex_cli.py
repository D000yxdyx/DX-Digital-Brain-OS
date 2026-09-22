"""Codex CLI execution provider (V0.2, Phase E.3B) -- the first REAL provider.

This module is the second implementation of the ``ExecutionProvider`` protocol in
``dxbrain.execution.contracts`` (the first being the offline ``FakeExecutionProvider``).
It turns one DX ``AgentExecution`` into exactly one ``codex exec`` child process.

Design rules this module is held to
-----------------------------------

**Spawn only.** ``dispatch()`` resolves the workspace, builds the argv, spawns the child,
hands the prompt over stdin, stores the process handle and returns immediately. It never
waits for Codex to finish. Waiting is ``collect()``'s job. The reason is frozen by the
outbox contract: the outbox lease lives in a *different* worker transaction, so a blocking
``dispatch()`` could let the lease expire while the AgentExecution still says ``requested``
and allow a duplicate real execution.

**Handoff semantics are the frozen ones.** ``handoff_attempted`` means "DX genuinely
created a provider child process", and nothing else:

* ``Popen`` raised before a child existed -> ``handoff_attempted=False`` -> the Gateway
  drives ``requested -> failed`` and the execution never reaches ``dispatched``.
* the child was created -> ``handoff_attempted=True``, regardless of what happens next.
  A later stdin-handoff failure therefore stays ``handoff_attempted=True`` with
  ``accepted=False``, which is the Gateway's existing "attempted but not accepted" path
  (``requested -> dispatched -> failed``). Returning ``False`` after a child exists would
  be a lie, and inventing a different lifecycle is forbidden.

**No DX internals.** Like ``contracts`` and ``fake``, this module imports no SQLAlchemy, no
ORM model and no ``dxbrain.core`` module: a provider receives plain dataclasses. It reaches
DX authority only through the injected ``WorkspaceResolver``.

**No credentials.** The child inherits the normal OS-user environment and reads its own
Codex credential store. DX never reads ``auth.json``, never injects a token or API key,
never logs a credential value and never touches ``CODEX_HOME``.

Process handle storage is ``PROCESS_LOCAL_EPHEMERAL`` (see ``ProcessHandles``): handles are
never persisted, so a worker crash leaves an orphan child and there is deliberately **no**
crash recovery or process-tree reaping in this experimental phase.
"""

from __future__ import annotations

import subprocess
from collections.abc import Mapping
from pathlib import Path

from dxbrain.execution.contracts import (
    OUTCOME_FAILED,
    OUTCOME_SUCCEEDED,
    DispatchReceipt,
    DispatchRequest,
    ExecutionOutcome,
    ExecutionProviderError,
    ProviderCollectionError,
)
from dxbrain.execution.workspace import WorkspaceDenied, WorkspaceResolver

# --------------------------------------------------------------------------------------
# Frozen / provider-local error_code vocabulary
# --------------------------------------------------------------------------------------
# Already in the E.2A vocabulary and reused here rather than duplicated:
#   provider_unavailable  the provider could not be reached or started
#   runtime_error         the provider ran and failed
# Three values are new in E.3B because no existing code expresses these outcomes. The
# AgentExecution.error_code column is String(64) with no value CHECK constraint, so no
# schema or migration is required. Adding a *guard* classification would be an ADR-level
# change; none of these are guard classifications -- they are provider outcomes.
PROVIDER_UNAVAILABLE = "provider_unavailable"
RUNTIME_ERROR = "runtime_error"
PROVIDER_STDIN_HANDOFF_FAILED = "provider_stdin_handoff_failed"
PROVIDER_DUPLICATE_DISPATCH = "provider_duplicate_dispatch"
PROVIDER_TIMEOUT = "provider_timeout"

# Same frozen string as ``dxbrain.execution.guard.WORKSPACE_DENIED``. Deliberately
# duplicated instead of imported: importing the guard would drag SQLAlchemy and the ORM
# into this provider module and break the provider/plumbing isolation described above.
WORKSPACE_DENIED = "workspace_denied"

# The one request_input key this provider reads.
PROMPT_KEY = "prompt"

# Bounded result persistence: the AgentExecution row is an execution fact ledger, not an
# artifact store. Only the tail of the captured output is ever persisted.
RESULT_TAIL_LIMIT = 4000

# Production defaults for E.3B. They are constructor parameters, not configuration and not
# task-controlled: nothing in DispatchRequest may widen how long a real child may run.
DEFAULT_TIMEOUT_SECONDS = 180.0
DEFAULT_TERMINATE_GRACE_SECONDS = 5.0

# The approved native executable type for the Windows E.3B smoke. npm shims (``codex.cmd``,
# ``codex.ps1``, the extension-less POSIX launcher) are rejected by design: Windows
# ``CreateProcess`` cannot execute a ``.cmd`` directly, and the npm shim needs a POSIX
# shell environment that a service process does not have.
APPROVED_EXECUTABLE_SUFFIX = ".exe"
_MZ_MAGIC = b"MZ"


def validate_native_executable(configured: str | None) -> str:
    """Return ``configured`` as a validated absolute path, or raise ``RuntimeError``.

    Fails fast during worker construction. DX never guesses or auto-discovers a Codex
    installation, and a misconfigured real mode must never decay into a fake success.
    """

    if not isinstance(configured, str) or not configured.strip():
        raise RuntimeError(
            "DX_CODEX_EXECUTABLE must be set to the approved native Codex executable when "
            "DX_AGENT_EXECUTION_ENABLED is true"
        )

    candidate = Path(configured.strip())
    if not candidate.is_absolute():
        # Same stance as WorkspaceResolver: a relative configured path would silently
        # anchor to the process working directory, which is exactly the fallback this
        # configuration must not have.
        raise RuntimeError(
            f"DX_CODEX_EXECUTABLE must be an absolute path, got {str(candidate)!r}"
        )
    if candidate.suffix.lower() != APPROVED_EXECUTABLE_SUFFIX:
        raise RuntimeError(
            f"DX_CODEX_EXECUTABLE must name the approved native executable "
            f"({APPROVED_EXECUTABLE_SUFFIX}); npm shims such as codex.cmd are not supported, "
            f"got {str(candidate)!r}"
        )
    if not candidate.is_file():
        raise RuntimeError(
            f"DX_CODEX_EXECUTABLE does not name an existing file: {str(candidate)!r}"
        )
    try:
        with candidate.open("rb") as stream:
            magic = stream.read(len(_MZ_MAGIC))
    except OSError as error:
        raise RuntimeError(
            f"DX_CODEX_EXECUTABLE could not be read: {str(candidate)!r}: {error}"
        ) from error
    if magic != _MZ_MAGIC:
        raise RuntimeError(
            f"DX_CODEX_EXECUTABLE does not look like a native Windows executable: "
            f"{str(candidate)!r}"
        )

    return str(candidate)


def build_argv(executable: str, workspace: Path) -> list[str]:
    """The exact E.3B argv. No flag is added merely because Codex supports it.

    ``-`` makes Codex read the prompt from stdin, so the prompt never appears in argv
    (where every other process on the machine could read it) and no Windows argv length
    limit applies.
    """

    return [
        executable,
        "exec",
        "--sandbox",
        "read-only",
        "--ask-for-approval",
        "never",
        "--color",
        "never",
        "--cd",
        str(workspace),
        "-",
    ]


def _decode(payload: bytes | None) -> str:
    """Decode captured output. Never raises: the tail is diagnostic, not a contract."""

    if not payload:
        return ""
    return payload.decode("utf-8", errors="replace")


def _bounded_tail(payload: bytes | None) -> str | None:
    """Return at most ``RESULT_TAIL_LIMIT`` characters from the END of the captured bytes."""

    text = _decode(payload).strip()
    if not text:
        return None
    return text[-RESULT_TAIL_LIMIT:]


class CodexCLIProvider:
    """One governed execution == one ``codex exec`` child process. (``ExecutionProvider``)"""

    name = "codex"

    def __init__(
        self,
        *,
        executable: str | None,
        workspace_resolver: WorkspaceResolver,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        terminate_grace_seconds: float = DEFAULT_TERMINATE_GRACE_SECONDS,
    ) -> None:
        # Validated here, not at dispatch time: an unusable executable is a startup
        # misconfiguration, so worker construction fails instead of every execution
        # failing one by one.
        self._executable = validate_native_executable(executable)
        self._workspace_resolver = workspace_resolver
        self._timeout_seconds = timeout_seconds
        self._terminate_grace_seconds = terminate_grace_seconds
        # PROCESS_LOCAL_EPHEMERAL. Keyed by external_execution_id. Never persisted.
        self._handles: dict[str, subprocess.Popen[bytes]] = {}

    # ----------------------------------------------------------------------------------
    # diagnostics
    # ----------------------------------------------------------------------------------

    @property
    def pending_handles(self) -> tuple[str, ...]:
        """The execution ids whose child process is still held by this provider instance."""

        return tuple(sorted(self._handles))

    @staticmethod
    def external_execution_id(execution_id: object) -> str:
        """Derive the provider-side correlation id from the canonical DX id.

        Deterministic and stable, matching ADR-0010's preference for a derivative of the
        DX execution id over a generated identifier. Never used to decide any DX state.
        """

        return f"codex-{execution_id}"

    # ----------------------------------------------------------------------------------
    # dispatch: SPAWN ONLY
    # ----------------------------------------------------------------------------------

    def dispatch(self, request: DispatchRequest) -> DispatchReceipt:
        external_execution_id = self.external_execution_id(request.execution_id)

        # A key that is already occupied means DX already created a child for this
        # execution in this process (which the Gateway's own idempotency check makes
        # unreachable in practice). This call creates nothing, so it is a PRE-handoff
        # refusal: expressed through the frozen receipt contract, exactly like the Fake
        # provider's rejections. No new exception type, no new lifecycle state.
        if external_execution_id in self._handles:
            return DispatchReceipt(
                handoff_attempted=False,
                accepted=False,
                external_execution_id=external_execution_id,
                error_code=PROVIDER_DUPLICATE_DISPATCH,
                error_detail=(
                    f"agent execution {request.execution_id} already has a live provider "
                    f"child in this process; refusing to create a second one"
                ),
            )

        # Last-moment re-resolution of the symbolic key, immediately before Popen, through
        # the SAME resolver instance the guard used. The workspace path never comes from
        # the AgentExecution row: only the symbolic key does, and only configured keys
        # resolve. No cwd / repo-root / HOME fallback exists in WorkspaceResolver.
        try:
            workspace = self._workspace_resolver.resolve(request.workspace_ref)
        except WorkspaceDenied as error:
            return DispatchReceipt(
                handoff_attempted=False,
                accepted=False,
                error_code=WORKSPACE_DENIED,
                error_detail=str(error),
            )

        # Extracted BEFORE spawning, so a malformed request can never leave an orphan child
        # behind. A missing prompt is a DX-side contract violation, not a provider outcome:
        # it raises and propagates with zero lifecycle writes, consistent with E.2A's rule
        # that only modelled provider outcomes are expressed as receipts.
        prompt = self._prompt_bytes(request)

        argv = build_argv(self._executable, workspace)

        try:
            process = subprocess.Popen(
                argv,
                cwd=str(workspace),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except OSError as error:
            # No child process exists: exactly the frozen pre-handoff failure, so the
            # execution goes requested -> failed and never reaches dispatched.
            return DispatchReceipt(
                handoff_attempted=False,
                accepted=False,
                error_code=PROVIDER_UNAVAILABLE,
                error_detail=f"codex exec could not be started: {error}",
            )

        # ------------------------------------------------------------------------------
        # From here the child EXISTS. 'dispatched' is already a DX fact and must not be
        # denied by anything below. The handle is stored BEFORE the stdin handoff so that
        # every failure path can still find, terminate, reap and forget the child.
        # ------------------------------------------------------------------------------
        self._handles[external_execution_id] = process

        try:
            self._hand_the_prompt_over(process, prompt)
        except (OSError, ValueError) as error:
            # Post-spawn handoff failure. The child is terminated and reaped so nothing
            # leaks, but handoff_attempted stays True. The Gateway's existing
            # "attempted but not accepted" path carries it: requested -> dispatched ->
            # failed. The environment is inherited unchanged.
            detail = self._terminate_and_reap(process)
            self._handles.pop(external_execution_id, None)
            return DispatchReceipt(
                handoff_attempted=True,
                accepted=False,
                external_execution_id=external_execution_id,
                error_code=PROVIDER_STDIN_HANDOFF_FAILED,
                error_detail=(
                    f"codex exec was started but the prompt could not be handed over: "
                    f"{error}; child cleanup: {detail}"
                ),
            )

        return DispatchReceipt(
            handoff_attempted=True,
            accepted=True,
            external_execution_id=external_execution_id,
        )

    # ----------------------------------------------------------------------------------
    # collect
    # ----------------------------------------------------------------------------------

    def collect(self, receipt: DispatchReceipt) -> ExecutionOutcome:
        key = receipt.external_execution_id
        process = self._handles.get(key) if isinstance(key, str) else None
        if process is None:
            # The frozen "expected operational failure" type: the Gateway maps it to
            # dispatched -> failed with error_code = provider_unavailable. A missing handle
            # means this process cannot see the child it is being asked to collect, which
            # is the documented consequence of PROCESS_LOCAL_EPHEMERAL handles.
            raise ProviderCollectionError(
                f"no in-process handle for external execution {key!r}; process-local "
                f"handles are not persisted and cannot survive a process boundary"
            )

        timed_out = False
        termination_detail = ""
        try:
            try:
                stdout, stderr = process.communicate(timeout=self._timeout_seconds)
            except subprocess.TimeoutExpired:
                timed_out = True
                termination_detail = self._terminate_and_reap(process)
                # Documented CPython pattern: after killing a timed-out child, call
                # communicate() again to drain what it already wrote.
                try:
                    stdout, stderr = process.communicate()
                except (OSError, ValueError):
                    stdout, stderr = b"", b""
        finally:
            # Every terminal collect path forgets the handle exactly once.
            self._handles.pop(key, None)

        result_tail = _bounded_tail(stdout) or _bounded_tail(stderr)

        if timed_out:
            # `started=True`: a child really was created and really ran (then was killed).
            # A timed-out execution must never claim a provider exit code, so exit_code is
            # left unset and the observed post-termination code is recorded in the detail.
            return ExecutionOutcome(
                external_execution_id=key,
                started=True,
                status=OUTCOME_FAILED,
                exit_code=None,
                summary="codex exec exceeded its timeout and was terminated",
                result_tail=result_tail,
                error_code=PROVIDER_TIMEOUT,
                error_detail=(
                    f"timeout_seconds={self._timeout_seconds}; "
                    f"returncode_after_termination={process.returncode}; {termination_detail}"
                ),
            )

        if process.returncode != 0:
            # Reuses the existing `runtime_error` classification: `exit_code` already
            # carries the provider's own exit status, so a new synonym would add nothing.
            return ExecutionOutcome(
                external_execution_id=key,
                started=True,
                status=OUTCOME_FAILED,
                exit_code=process.returncode,
                summary="codex exec exited with a non-zero status",
                result_tail=result_tail,
                error_code=RUNTIME_ERROR,
                error_detail=f"exit_code={process.returncode}",
            )

        return ExecutionOutcome(
            external_execution_id=key,
            started=True,
            status=OUTCOME_SUCCEEDED,
            exit_code=0,
            summary="codex exec completed",
            result_tail=result_tail,
        )

    # ----------------------------------------------------------------------------------
    # internals
    # ----------------------------------------------------------------------------------

    @staticmethod
    def _prompt_bytes(request: DispatchRequest) -> bytes:
        """The prompt for this execution, taken from dispatch input only."""

        request_input = request.request_input
        prompt = (
            request_input.get(PROMPT_KEY) if isinstance(request_input, Mapping) else None
        )
        if not isinstance(prompt, str) or not prompt.strip():
            raise ExecutionProviderError(
                f"agent execution {request.execution_id} carries no usable "
                f"{PROMPT_KEY!r} string in request_input"
            )
        return prompt.encode("utf-8")

    @staticmethod
    def _hand_the_prompt_over(process: subprocess.Popen[bytes], prompt: bytes) -> None:
        """Write the prompt, then make the stream unusable-but-harmless for ``collect()``.

        Handing stdin over is finished inside ``dispatch()`` so the child is never waiting
        on DX. ``collect()`` later calls ``communicate()`` on the same ``Popen``; leaving a
        *closed* file object on ``process.stdin`` would make CPython try to flush a closed
        stream, which raises ``ValueError`` instead of returning the captured output.
        Clearing the attribute tells the library there is no remaining stdin to manage.
        ``tests/test_execution_codex_provider.py`` proves this rather than assuming it.
        """

        stdin = process.stdin
        if stdin is None:
            raise OSError("codex exec was started without a stdin pipe")
        try:
            stdin.write(prompt)
            stdin.flush()
        finally:
            stdin.close()
        process.stdin = None  # type: ignore[assignment]

    def _terminate_and_reap(self, process: subprocess.Popen[bytes]) -> str:
        """End a live child and reap it. Returns a diagnostic note for the audit trail.

        On Windows ``Popen.terminate()`` calls ``TerminateProcess`` on the direct child,
        which is unconditional -- there is no graceful, POSIX-style shutdown to wait for.
        The bounded grace period and the ``kill()`` escalation are therefore kept as a
        code path (and exercised by a test), not as a claim about graceful termination.
        Descendant processes are NOT managed: process-tree handling is out of scope for
        this phase, and Codex may start helper executables of its own.
        """

        if process.poll() is not None:
            return f"child had already exited with code {process.returncode}"

        notes: list[str] = []
        try:
            process.terminate()
        except OSError as error:  # pragma: no cover - Windows terminate rarely fails
            notes.append(f"terminate failed: {error}")

        try:
            process.wait(timeout=self._terminate_grace_seconds)
            notes.append("child terminated")
            return "; ".join(notes)
        except subprocess.TimeoutExpired:
            notes.append("terminate did not end the child within the grace period")

        try:
            process.kill()
            process.wait()
            notes.append("child killed")
        except OSError as error:  # pragma: no cover - kill is the last resort
            notes.append(f"kill failed: {error}")
        return "; ".join(notes)
