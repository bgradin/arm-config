from __future__ import annotations

import os
import re
import shutil
from collections import defaultdict
from pathlib import Path
from typing import Any

from .config import Config
from .db import Database
from .util import ensure_within, hash_file, hash_json, safe_component, utc_now
from .workflow import approval_errors, approval_fingerprint


class PlanError(RuntimeError):
    pass


class CommitError(RuntimeError):
    pass


_LIBRARY_EPISODE = re.compile(
    r"S(?P<season>\d+)E(?P<start>\d+)(?:-?E(?P<end>\d+))?",
    re.IGNORECASE,
)


def _library_snapshot(show_root: Path) -> dict[str, Any]:
    files = []
    if show_root.exists():
        for path in sorted(item for item in show_root.rglob("*") if item.is_file()):
            stat = path.stat()
            files.append(
                {
                    "path": path.relative_to(show_root).as_posix(),
                    "size_bytes": stat.st_size,
                    "mtime_ns": stat.st_mtime_ns,
                    "device": stat.st_dev,
                    "inode": stat.st_ino,
                }
            )
    return {"hash": hash_json(files), "files": files}


def _occupied_episodes(snapshot: dict[str, Any]) -> set[tuple[int, int]]:
    occupied = set()
    for item in snapshot["files"]:
        match = _LIBRARY_EPISODE.search(Path(item["path"]).name)
        if not match:
            continue
        season = int(match.group("season"))
        start = int(match.group("start"))
        end = int(match.group("end") or start)
        occupied.update((season, episode) for episode in range(start, end + 1))
    return occupied


def _restore_target(
    target: Path,
    source: Path,
    expected_hash: str | None,
) -> None:
    """Restore a moved target to staging, including across filesystems."""

    if expected_hash and hash_file(target) != expected_hash:
        raise CommitError(f"Interrupted target hash does not match plan: {target}")
    source.parent.mkdir(parents=True, exist_ok=True)
    try:
        if target.stat().st_dev == source.parent.stat().st_dev:
            os.replace(target, source)
            return
    except OSError:
        pass
    temporary = source.with_name(f".{source.name}.restore.tmp")
    if temporary.exists():
        temporary.unlink()
    shutil.copy2(target, temporary)
    if expected_hash and hash_file(temporary) != expected_hash:
        temporary.unlink(missing_ok=True)
        raise CommitError(f"Restored source hash does not match plan: {source}")
    with temporary.open("rb") as handle:
        os.fsync(handle.fileno())
    os.replace(temporary, source)
    target.unlink()


def _recover_interrupted_commit(
    database: Database,
    job_id: str,
    plan_record: dict[str, Any],
) -> None:
    """Compensate filesystem work left between durable plan and DB commit."""

    recovered = []
    for operation in reversed(plan_record["plan"]["operations"]):
        if operation.get("action") != "move":
            continue
        source = Path(operation["source"])
        target = Path(operation["target"])
        consumed = source.with_name(f".{source.name}.{job_id}.consumed")
        if consumed.exists():
            if source.exists():
                raise CommitError(
                    f"Both source and rollback copy exist after interruption: {source}"
                )
            os.replace(consumed, source)
            if target.exists():
                if operation.get("sha256") and hash_file(target) != operation["sha256"]:
                    raise CommitError(f"Interrupted target hash does not match plan: {target}")
                target.unlink()
            recovered.append(str(source))
        elif target.exists() and source.exists():
            if operation.get("sha256") and hash_file(target) != operation["sha256"]:
                raise CommitError(f"Interrupted target hash does not match plan: {target}")
            target.unlink()
            recovered.append(str(source))
        elif target.exists() and not source.exists():
            _restore_target(target, source, operation.get("sha256"))
            recovered.append(str(source))
        elif not source.exists():
            raise CommitError(
                f"Neither source nor target exists for interrupted operation: {source}"
            )
    database.audit(
        "plan.interrupted_commit_recovered",
        {"plan_id": plan_record["id"], "sources": recovered},
        job_id,
        "worker",
    )
    database.transition(job_id, "approved", actor="worker")


def _show_components(job: dict[str, Any]) -> tuple[str, str]:
    show = safe_component(str(job["show_name"]))
    year = f" ({int(job['show_year'])})" if job.get("show_year") else ""
    provider = str(job.get("show_provider") or "tmdb").lower()
    identifier = safe_component(str(job["show_id"]))
    folder = f"{show}{year} [{provider}id-{identifier}]"
    filename = f"{show}{year}"
    return folder, filename


def _episode_token(assignment: dict[str, Any]) -> str:
    season = int(assignment["season"])
    start = int(assignment["episode_start"])
    end = assignment.get("episode_end")
    if end is not None and int(end) != start:
        return f"S{season:02d}E{start:02d}-E{int(end):02d}"
    return f"S{season:02d}E{start:02d}"


def build_plan(config: Config, database: Database, job_id: str) -> dict[str, Any]:
    errors = approval_errors(config, database, job_id)
    if errors:
        raise PlanError("\n".join(errors))
    job = database.get_job(job_id)
    if job is None:
        raise PlanError("Job does not exist")
    if job["state"] not in {"needs_review", "approved"}:
        raise PlanError(f"Job must be reviewable, not {job['state']}")
    assets = database.list_assets(job_id)
    assignments = {
        item["asset_id"]: item for item in database.list_assignments(job_id)
    }
    show_folder, filename_prefix = _show_components(job)
    show_root = ensure_within(config.library_root / show_folder, config.library_root)
    library_snapshot = _library_snapshot(show_root)
    occupied = _occupied_episodes(library_snapshot)
    groups: dict[tuple[int, int], list[dict[str, Any]]] = defaultdict(list)
    for asset in assets:
        assignment = assignments.get(asset["id"])
        if asset["disposition"] == "episode" and assignment:
            groups[(int(assignment["season"]), int(assignment["episode_start"]))].append(
                asset
            )

    operations = []
    target_keys = set()
    planned_episodes = set()
    for asset in assets:
        source = Path(asset["path"])
        disposition = asset["disposition"]
        operation: dict[str, Any] = {
            "asset_id": asset["id"],
            "source": str(source),
            "size_bytes": asset["size_bytes"],
            "sha256": asset.get("sha256"),
            "disposition": disposition,
        }
        if disposition in {"ignore", "duplicate"}:
            operation["action"] = "retain"
            operation["reason"] = disposition
            operations.append(operation)
            continue
        if disposition == "extra":
            season_root = show_root / f"Season {int(job['season']):02d}" / "extras"
            target = season_root / safe_component(source.name)
        elif disposition == "episode":
            assignment = assignments[asset["id"]]
            episode_start = int(assignment["episode_start"])
            episode_end = int(assignment.get("episode_end") or episode_start)
            planned_episodes.update(
                (int(assignment["season"]), episode)
                for episode in range(episode_start, episode_end + 1)
            )
            key = (int(assignment["season"]), int(assignment["episode_start"]))
            versions = groups[key]
            version_assignments = [assignments[item["id"]] for item in versions]
            part_values = [item.get("part") for item in version_assignments]
            multipart = (
                len(versions) > 1
                and all(part is not None for part in part_values)
                and len(set(part_values)) == len(part_values)
            )
            if (
                len(versions) > 1
                and not multipart
                and not config.jellyfin_episode_versions
            ):
                if not asset["preferred"]:
                    operation["action"] = "retain"
                    operation["reason"] = "non_preferred_edition"
                    operations.append(operation)
                    continue
            token = _episode_token(assignment)
            title = (
                f" {safe_component(assignment['episode_title'])}"
                if assignment.get("episode_title")
                else ""
            )
            edition = ""
            if (
                len(versions) > 1
                and not multipart
                and config.jellyfin_episode_versions
            ):
                edition_name = asset.get("edition_name") or assignment.get(
                    "edition_name"
                )
                if not edition_name:
                    raise PlanError(
                        f"Asset {source} needs an edition name for version export"
                    )
                edition = f" - {safe_component(edition_name)}"
            part = (
                f"-part-{int(assignment['part'])}"
                if assignment.get("part") is not None
                else ""
            )
            filename = (
                f"{filename_prefix} {token}{title}{edition}{part}{source.suffix.lower()}"
            )
            target = show_root / f"Season {int(assignment['season']):02d}" / filename
        else:
            raise PlanError(f"Unsupported disposition {disposition!r}")
        target = ensure_within(target, config.library_root)
        target_key = str(target).casefold()
        if target_key in target_keys:
            raise PlanError(f"Multiple assets target the same path: {target}")
        target_keys.add(target_key)
        operation["action"] = "move"
        operation["target"] = str(target)
        operation["conflict"] = target.exists()
        operations.append(operation)

    conflicts = [item["target"] for item in operations if item.get("conflict")]
    if conflicts:
        raise PlanError("Target paths already exist:\n" + "\n".join(conflicts))
    episode_conflicts = sorted(planned_episodes & occupied)
    if episode_conflicts:
        formatted = ", ".join(
            f"S{season:02d}E{episode:02d}" for season, episode in episode_conflicts
        )
        raise PlanError(f"Library already contains assigned episodes: {formatted}")
    plan = {
        "schema_version": 1,
        "job_id": job_id,
        "manifest_hash": job["manifest_hash"],
        "approval_fingerprint": approval_fingerprint(database, job_id),
        "created_at": utc_now(),
        "library_root": str(config.library_root),
        "show_root": str(show_root),
        "library_snapshot": library_snapshot,
        "jellyfin_episode_versions": config.jellyfin_episode_versions,
        "operations": operations,
    }
    plan["id"] = database.save_plan(job_id, plan)
    database.audit(
        "plan.created",
        {"plan_id": plan["id"], "operations": len(operations)},
        job_id,
        "user",
    )
    return plan


def _validate_operation(operation: dict[str, Any], config: Config) -> tuple[Path, Path]:
    source = Path(operation["source"])
    target = ensure_within(Path(operation["target"]), config.library_root)
    if not source.is_file():
        raise CommitError(f"Source file is missing: {source}")
    if source.stat().st_size != int(operation["size_bytes"]):
        raise CommitError(f"Source file size changed: {source}")
    if operation.get("sha256") and hash_file(source) != operation["sha256"]:
        raise CommitError(f"Source file hash changed: {source}")
    if target.exists():
        raise CommitError(f"Target already exists: {target}")
    return source, target


def commit_plan(
    config: Config,
    database: Database,
    job_id: str,
    plan_id: str | None = None,
) -> dict[str, Any]:
    job = database.get_job(job_id)
    if job is None:
        raise KeyError(job_id)
    plan_record = database.get_plan(plan_id) if plan_id else database.latest_plan(job_id)
    if not plan_record:
        raise CommitError("No organization plan exists")
    if job["state"] == "complete" and plan_record["status"] == "committed":
        operations = plan_record["plan"]["operations"]
        return {
            "plan_id": plan_record["id"],
            "moved": sum(item.get("action") == "move" for item in operations),
            "retained": sum(item.get("action") == "retain" for item in operations),
            "already_complete": True,
        }
    if job["state"] == "organizing" and plan_record["status"] == "draft":
        _recover_interrupted_commit(database, job_id, plan_record)
        job = database.get_job(job_id)
        assert job is not None
    if job["state"] != "approved":
        raise CommitError(f"Job must be approved, not {job['state']}")
    if plan_record["status"] != "draft":
        raise CommitError(f"Plan is already {plan_record['status']}")
    plan = plan_record["plan"]
    if plan["manifest_hash"] != job["manifest_hash"]:
        raise CommitError("Plan manifest hash no longer matches the job")
    if plan.get("approval_fingerprint") != approval_fingerprint(database, job_id):
        raise CommitError("Review decisions changed after this plan was approved")
    current_snapshot = _library_snapshot(Path(plan["show_root"]))
    if current_snapshot["hash"] != plan.get("library_snapshot", {}).get("hash"):
        raise CommitError("Library contents changed after the dry-run plan was created")
    moves = [item for item in plan["operations"] if item["action"] == "move"]
    validated = [(item, *_validate_operation(item, config)) for item in moves]
    database.transition(job_id, "organizing", actor="user")

    journal: list[dict[str, Any]] = []
    try:
        for operation, source, target in validated:
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                same_filesystem = source.stat().st_dev == target.parent.stat().st_dev
            except OSError:
                same_filesystem = False
            if same_filesystem:
                os.replace(source, target)
                journal.append(
                    {"method": "rename", "source": source, "target": target}
                )
            else:
                temporary = target.with_name(f".{target.name}.{job_id}.tmp")
                if temporary.exists():
                    temporary.unlink()
                shutil.copy2(source, temporary)
                expected = operation.get("sha256") or hash_file(source)
                if hash_file(temporary) != expected:
                    temporary.unlink(missing_ok=True)
                    raise CommitError(f"Copied file hash mismatch: {target}")
                with temporary.open("rb") as handle:
                    os.fsync(handle.fileno())
                os.replace(temporary, target)
                journal.append(
                    {
                        "method": "copy",
                        "source": source,
                        "target": target,
                        "consumed": None,
                    }
                )
        for item in journal:
            if item["method"] != "copy":
                continue
            source = item["source"]
            consumed = source.with_name(f".{source.name}.{job_id}.consumed")
            if consumed.exists():
                raise CommitError(f"Consumed-source staging path exists: {consumed}")
            os.replace(source, consumed)
            item["consumed"] = consumed
        database.complete_plan(
            plan_record["id"],
            job_id,
            [
                {
                    "method": item["method"],
                    "source": str(item["source"]),
                    "target": str(item["target"]),
                }
                for item in journal
            ],
        )
        cleanup_errors = []
        for item in journal:
            consumed = item.get("consumed")
            if not consumed:
                continue
            try:
                consumed.unlink()
            except OSError as cleanup_exc:
                cleanup_errors.append(str(cleanup_exc))
        if cleanup_errors:
            database.audit(
                "plan.cleanup_deferred",
                {"errors": cleanup_errors},
                job_id,
                "system",
            )
        return {
            "plan_id": plan_record["id"],
            "moved": len(journal),
            "retained": len(plan["operations"]) - len(journal),
        }
    except Exception as exc:
        rollback_errors = []
        for item in reversed(journal):
            source = item["source"]
            target = item["target"]
            try:
                consumed = item.get("consumed")
                if consumed and consumed.exists() and not source.exists():
                    os.replace(consumed, source)
                if item["method"] == "rename" and target.exists() and not source.exists():
                    source.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(target, source)
                elif item["method"] == "copy" and target.exists():
                    target.unlink()
            except OSError as rollback_exc:
                rollback_errors.append(str(rollback_exc))
        message = str(exc)
        if rollback_errors:
            message += "; rollback errors: " + "; ".join(rollback_errors)
        database.audit(
            "plan.commit_failed",
            {"plan_id": plan_record["id"], "error": message},
            job_id,
            "system",
        )
        database.transition(job_id, "approved", error=message)
        raise CommitError(message) from exc