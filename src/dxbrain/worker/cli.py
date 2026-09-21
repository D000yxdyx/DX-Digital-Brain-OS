from __future__ import annotations

from collections.abc import Callable
from uuid import UUID

from dxbrain.config import WorkerSettings
from dxbrain.core.database import create_session_factory
from dxbrain.execution.fake import FakeExecutionProvider
from dxbrain.execution.gateway import ExecutionGateway
from dxbrain.worker.outbox import OutboxWorker


def build_execution_handler(settings: WorkerSettings) -> Callable[[UUID], None] | None:
    """Wire the Execution Gateway on its own principal, or do not wire it at all.

    ``DX_EXECUTION_DATABASE_URL`` is optional. When it is absent this returns ``None``
    and the worker keeps its original behaviour: there is deliberately no fallback to
    the ``dx_worker`` credential, so the Gateway can only ever run as ``dx_api``.

    The Gateway is the only component that receives a session factory built from the
    execution URL; the worker's own factory is built from ``DX_WORKER_DATABASE_URL``
    and is never handed to the Gateway.
    """

    if settings.execution_database_url is None:
        return None
    gateway = ExecutionGateway(
        create_session_factory(settings.execution_database_url),
        FakeExecutionProvider(),
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
