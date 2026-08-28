from __future__ import annotations

from dxbrain.config import WorkerSettings
from dxbrain.core.database import create_session_factory
from dxbrain.worker.outbox import OutboxWorker


def main() -> None:
    """Consume one durable internal event and exit; scheduler policy is deferred."""

    settings = WorkerSettings()
    event_id = OutboxWorker(create_session_factory(settings.database_url)).consume_one()
    if event_id is None:
        print("no pending event")
    else:
        print(f"consumed {event_id}")


if __name__ == "__main__":
    main()
