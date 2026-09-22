from __future__ import annotations

from collections.abc import Callable
from uuid import UUID

from dxbrain.config import WorkerSettings
from dxbrain.core.database import create_session_factory
from dxbrain.execution.codex_cli import CodexCLIProvider
from dxbrain.execution.fake import FakeExecutionProvider
from dxbrain.execution.gateway import ExecutionGateway
from dxbrain.execution.guard import build_pre_dispatch_guard
from dxbrain.execution.workspace import WorkspaceResolver
from dxbrain.worker.outbox import OutboxWorker


def build_execution_handler(settings: WorkerSettings) -> Callable[[UUID], None] | None:
    """Wire the Execution Gateway on its own principal, or do not wire it at all.

    ``DX_EXECUTION_DATABASE_URL`` is optional. When it is absent this returns ``None``
    and the worker keeps its original behaviour: there is deliberately no fallback to
    the ``dx_worker`` credential, so the Gateway can only ever run as ``dx_api``.

    The Gateway is the only component that receives a session factory built from the
    execution URL; the worker's own factory is built from ``DX_WORKER_DATABASE_URL``
    and is never handed to the Gateway.

    Phase E.3B wires the two things the E.3A safety foundation added but never connected
    to this runtime:

    * **the pre-dispatch guard**, always. ``guard=None`` is reserved for the offline E.2A
      runtime and for tests; in the worker it is never None, because without it the
      ``DX_AGENT_EXECUTION_ENABLED`` kill switch would be inert and every approval, agent
      binding and workspace check would be skipped.
    * **the real provider**, only when the switch is on. This is a two-branch choice
      driven by the switch -- not a registry, not a provider dictionary and not a plug-in
      framework. When the switch is on, ``CodexCLIProvider`` validates
      ``DX_CODEX_EXECUTABLE`` in its constructor and raises, so a misconfigured real mode
      stops the worker instead of silently decaying into the Fake provider.
    """

    if settings.execution_database_url is None:
        return None
    execution_session_factory = create_session_factory(settings.execution_database_url)

    # ONE resolver instance, shared by the guard's authority check and the provider's
    # last-moment re-resolution immediately before Popen. WorkspaceResolver copies the
    # mapping, so authority cannot be widened between the two calls.
    workspace_resolver = WorkspaceResolver(settings.agent_workspaces)

    if settings.agent_execution_enabled:
        provider = CodexCLIProvider(
            executable=settings.codex_executable,
            workspace_resolver=workspace_resolver,
            disabled_mcp_servers=tuple(settings.codex_disabled_mcp_servers),
        )
    else:
        provider = FakeExecutionProvider()

    gateway = ExecutionGateway(
        execution_session_factory,
        provider,
        guard=build_pre_dispatch_guard(settings, execution_session_factory),
    )
    return gateway.handle_event


def main() -> None:
    """Consume one durable internal event and exit; scheduler policy is deferred."""

    settings = WorkerSettings()
    worker = OutboxWorker(
        create_session_factory(settings.database_url),
        handler=build_execution_handler(settings),
    )
    event_id = worker.consume_one()
    if event_id is None:
        print("no pending event")
    else:
        print(f"consumed {event_id}")


if __name__ == "__main__":
    main()
