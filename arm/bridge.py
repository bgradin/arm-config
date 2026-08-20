"""Narrow bridge from ARM's privileged lifecycle to tv organizer persistence."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Any

from tv.organizer.collector import collect_disc
from tv.organizer.config import Config
from tv.organizer.db import Database
from tv.organizer.importer import register_manifest
from tv.organizer.util import atomic_json_write


LOGGER = logging.getLogger(__name__)

MOUNT_READY_TIMEOUT_SECONDS = 60.0
MOUNT_RETRY_INTERVAL_SECONDS = 2.0


def _first(job: Any, *names: str) -> Any:
    for name in names:
        value = getattr(job, name, None)
        if value not in (None, ""):
            return value
    return None


def _arm_job_id(job: Any) -> str:
    value = _first(job, "job_id", "jobid", "id")
    if value is None:
        raise RuntimeError("ARM job exposes no stable job identifier")
    return str(value)


def _mounted_root(job: Any) -> Path:
    value = _first(job, "mountpoint", "mount_point", "mount_path")
    if not value:
        raise RuntimeError("ARM job exposes no mounted disc path")
    path = Path(str(value))
    if not path.is_dir():
        raise RuntimeError(f"ARM disc mount is not readable: {path}")

    # ARM can report a successful mount before the optical drive has exposed
    # its filesystem tree. It can also lose the mount while the drive is
    # still spinning up. Do not hand an empty mountpoint to the collector:
    # retry the same fstab-backed mount operation and require the navigation
    # directory to be visible before continuing.
    device_value = _first(job, "devpath", "dev_path", "device")
    device = None
    if device_value:
        device = str(device_value)
        if not device.startswith("/"):
            device = f"/dev/{device}"

    deadline = time.monotonic() + MOUNT_READY_TIMEOUT_SECONDS
    last_mount_error = "mount was not retried"
    while True:
        if any((path / directory).is_dir() for directory in ("BDMV", "VIDEO_TS")):
            return path

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError(
                f"ARM mount {path} did not expose BDMV or VIDEO_TS within "
                f"{MOUNT_READY_TIMEOUT_SECONDS:.0f}s ({last_mount_error})"
            )

        if device:
            try:
                result = subprocess.run(
                    ["mount", "--source", device],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=min(10.0, remaining),
                )
                output = (result.stdout or result.stderr or "").strip()
                if result.returncode:
                    last_mount_error = output or f"mount exited {result.returncode}"
                else:
                    last_mount_error = output or "mount command succeeded"
            except (OSError, subprocess.SubprocessError) as exc:
                last_mount_error = str(exc)

        time.sleep(min(MOUNT_RETRY_INTERVAL_SECONDS, remaining))


def _rip_root(job: Any) -> Path:
    value = _first(job, "path", "output_path", "destination")
    if not value:
        raise RuntimeError("ARM job exposes no completed rip path")
    path = Path(str(value)).resolve()
    if not path.is_dir():
        raise RuntimeError(f"ARM completed rip path is not readable: {path}")
    return path


def _makemkv_source(job: Any) -> str | None:
    value = _first(job, "devpath", "dev_path", "device")
    return f"dev:{value}" if value else None


def _handoff_path(config: Config, arm_job_id: str) -> Path:
    # ARM identifiers are normally integers; sanitizing also makes the path
    # safe if an upstream release changes that representation.
    safe = "".join(char for char in arm_job_id if char.isalnum() or char in "-_")
    if not safe:
        raise RuntimeError("ARM job identifier cannot be represented safely")
    return config.data_root / "arm-jobs" / f"{safe}.json"


def _database(config: Config) -> Database:
    config.ensure_directories()
    database = Database(config.database_path)
    database.initialize()
    return database


def capture_for_arm(job: Any) -> None:
    """Capture a mounted video disc and register its durable organizer job."""

    # ``video_type`` is ARM's movie/series classification, not the physical
    # format.  Never use it to filter here: this service must independently
    # decide whether the captured video disc contains TV episodes.
    media_type = str(_first(job, "media_type") or "").lower()
    if media_type in {"audio", "music", "data"}:
        LOGGER.info("Skipping organizer capture for ARM media type %s", media_type)
        return

    config = Config.from_env()
    arm_job_id = _arm_job_id(job)
    handoff = _handoff_path(config, arm_job_id)
    if handoff.is_file():
        LOGGER.info("Capture already exists for ARM job %s", arm_job_id)
        return
    manifest_path = collect_disc(
        config,
        _mounted_root(job),
        arm_job_id=arm_job_id,
        makemkv_source=_makemkv_source(job),
        volume_label=str(_first(job, "label", "title", "disc_label") or "") or None,
        producer_versions={
            "arm": str(
                _first(job, "arm_version")
                or os.environ.get("ARM_VERSION", "unknown")
            ),
            "ripper": "makemkvcon",
        },
        include_menu_assets=os.environ.get(
            "TV_CAPTURE_MENU_ASSETS", ""
        ).lower() in {"1", "true", "yes", "on"},
    )
    database = _database(config)
    organizer_job_id = register_manifest(
        database,
        manifest_path,
        state="ripping",
    )
    atomic_json_write(
        handoff,
        {
            "arm_job_id": arm_job_id,
            "organizer_job_id": organizer_job_id,
            "manifest_path": str(manifest_path),
        },
    )
    database.audit(
        "arm.capture_complete",
        {"arm_job_id": arm_job_id, "manifest_path": str(manifest_path)},
        organizer_job_id,
        actor="arm-bridge",
    )


def complete_for_arm(job: Any) -> None:
    """Turn ARM's explicit rip completion into a durable asset-import task."""

    config = Config.from_env()
    arm_job_id = _arm_job_id(job)
    handoff_path = _handoff_path(config, arm_job_id)
    if not handoff_path.is_file():
        LOGGER.info("No organizer capture exists for ARM job %s", arm_job_id)
        return

    handoff = json.loads(handoff_path.read_text(encoding="utf-8"))
    if handoff.get("completion_task_id"):
        LOGGER.info("ARM job %s was already handed off", arm_job_id)
        return
    organizer_job_id = str(handoff["organizer_job_id"])
    rip_root = _rip_root(job)
    database = _database(config)
    task_id = database.enqueue_unique(
        "attach_assets",
        organizer_job_id,
        {"rip_root": str(rip_root)},
    )
    handoff["completion_task_id"] = task_id
    handoff["rip_root"] = str(rip_root)
    atomic_json_write(handoff_path, handoff)
    database.audit(
        "arm.rip_complete",
        {
            "arm_job_id": arm_job_id,
            "rip_root": str(rip_root),
            "task_id": task_id,
        },
        organizer_job_id,
        actor="arm-bridge",
    )
