from __future__ import annotations

import threading
from pathlib import Path

from .analyzers import analyze_job
from .config import Config
from .db import Database
from .importer import attach_assets
from .organizer import organize_assets
from .planner import build_plan, commit_plan


class Worker:
    def __init__(self, config: Config, database: Database):
        self.config = config
        self.database = database
        self._stop = threading.Event()
        self.database.recover_running_tasks()

    def stop(self) -> None:
        self._stop.set()

    def run_once(self) -> bool:
        task = self.database.claim_task()
        if task is None:
            return False
        error = None
        try:
            if task["kind"] == "analyze":
                analyze_job(self.config, self.database, task["job_id"])
            elif task["kind"] == "attach_assets":
                attach_assets(
                    self.config,
                    self.database,
                    task["job_id"],
                    Path(task["payload"]["rip_root"]),
                    enqueue_analysis=True,
                )
            elif task["kind"] == "plan":
                build_plan(self.config, self.database, task["job_id"])
            elif task["kind"] == "commit":
                commit_plan(
                    self.config,
                    self.database,
                    task["job_id"],
                    task["payload"].get("plan_id"),
                )
            elif task["kind"] == "organize":
                organize_assets(self.config, self.database, task["job_id"])
            else:
                raise ValueError(f"Unknown task kind: {task['kind']}")
        except Exception as exc:
            error = str(exc)
            if task["kind"] == "organize" and task.get("job_id"):
                job = self.database.get_job(task["job_id"])
                if job and job["state"] == "organizing":
                    try:
                        self.database.transition(
                            task["job_id"], "failed", error=error, actor="worker"
                        )
                    except ValueError:
                        pass
        self.database.finish_task(task["id"], error)
        return True

    def run_forever(self) -> None:
        while not self._stop.is_set():
            if not self.run_once():
                self._stop.wait(self.config.poll_seconds)

    def start_thread(self) -> threading.Thread:
        thread = threading.Thread(
            target=self.run_forever,
            name="tv-worker",
            daemon=True,
        )
        thread.start()
        return thread
