import time
import uuid

from .config import Settings
from .db import Database
from .hermes_adapter import HermesCliAdapterV1


class Worker:
    def __init__(self, db: Database, settings: Settings, worker_id: str | None = None):
        self.db = db
        self.settings = settings
        self.worker_id = worker_id or str(uuid.uuid4())
        self.adapter = HermesCliAdapterV1(settings.hermes_binary, settings.task_timeout_seconds)

    def run_once(self) -> bool:
        if not self.settings.legacy_worker_mode:
            return False
        task = self.db.lease_task(self.worker_id)
        if not task:
            return False
        self.db.event("task.running", task["correlation_id"], {"task_id": task["id"], "worker_id": self.worker_id})
        result = self.adapter.execute(task["profile"], task["prompt"])
        self.db.finish_task(task["id"], result.status, result.output, result.error)
        self.db.event(f"task.{result.status}", task["correlation_id"], {"task_id": task["id"], "exit_code": result.exit_code})
        return True

    def run_forever(self, poll_seconds: float = 0.5) -> None:
        while True:
            if not self.run_once():
                time.sleep(poll_seconds)


def main() -> None:
    settings = Settings()
    if not settings.legacy_worker_mode:
        raise SystemExit("Legacy direct worker is disabled. Set HFC_LEGACY_WORKER_MODE=true for one-release compatibility mode.")
    Worker(Database(settings.database_path), settings).run_forever()


if __name__ == "__main__":
    main()
