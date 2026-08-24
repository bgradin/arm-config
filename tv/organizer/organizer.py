"""Background, idempotent organization of reviewed episode assets."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any

from .config import Config
from .db import Database
from .util import ensure_within, hash_file, safe_component


class OrganizationError(RuntimeError):
    """Raised when an episode cannot be moved safely."""


def _show_folder(job: dict[str, Any]) -> Path:
    show = safe_component(str(job["show_name"]))
    year = f" ({int(job['show_year'])})" if job.get("show_year") else ""
    provider = safe_component(str(job.get("show_provider") or "tmdb").lower())
    identifier = safe_component(str(job["show_id"]))
    return Path(f"{show}{year} [{provider}id-{identifier}]")


def _episode_token(assignment: dict[str, Any]) -> str:
    season = int(assignment["season"])
    start = int(assignment["episode_start"])
    end = assignment.get("episode_end")
    if end is not None and int(end) != start:
        return f"S{season:02d}E{start:02d}-E{int(end):02d}"
    return f"S{season:02d}E{start:02d}"


def _target_for(
    config: Config,
    job: dict[str, Any],
    asset: dict[str, Any],
    assignment: dict[str, Any],
    group: list[dict[str, Any]],
) -> Path:
    assignment = {
        **assignment,
        "season": assignment.get("season") or job.get("season"),
    }
    season = int(assignment["season"])
    token = _episode_token(assignment)
    title = ""
    if assignment.get("episode_title"):
        title = f" - {safe_component(str(assignment['episode_title']))}"

    # Jellyfin treats the same episode number as one item unless versions are
    # explicitly named. Parts are always distinct files. A collision is left
    # for the organizer to report instead of silently replacing a file.
    edition = ""
    part = ""
    if assignment.get("part") is not None:
        part = f"-part-{int(assignment['part'])}"
    elif len(group) > 1:
        edition_name = asset.get("edition_name") or assignment.get("edition_name")
        if edition_name:
            edition = f" - {safe_component(str(edition_name))}"
        elif config.jellyfin_episode_versions:
            raise OrganizationError(
                f"Asset {asset['path']} needs an edition name for episode versions"
            )

    filename = f"{_episode_token(assignment)}{title}{edition}{part}"
    suffix = Path(asset["path"]).suffix.lower()
    return ensure_within(
        config.library_root
        / _show_folder(job)
        / f"Season {season:02d}"
        / f"{filename}{suffix}",
        config.library_root,
    )


def _verify(path: Path, size_bytes: int, sha256: str | None) -> None:
    if not path.is_file():
        raise OrganizationError(f"Moved target is missing: {path}")
    if path.stat().st_size != int(size_bytes):
        raise OrganizationError(f"Moved target size changed: {path}")
    if sha256 and hash_file(path) != sha256:
        raise OrganizationError(f"Moved target hash changed: {path}")


def _move_one(
    config: Config,
    database: Database,
    job_id: str,
    asset: dict[str, Any],
    assignment: dict[str, Any],
    group: list[dict[str, Any]],
) -> dict[str, Any]:
    source = Path(asset["path"]).resolve()
    target = _target_for(config, database.get_job(job_id) or {}, asset, assignment, group)
    source_allowed = False
    for root in (config.inbox_root, config.library_root):
        try:
            ensure_within(source, root)
            source_allowed = True
            break
        except ValueError:
            continue
    if not source_allowed:
        raise OrganizationError(f"Asset source is outside configured roots: {source}")

    move = database.prepare_move(
        job_id,
        asset["id"],
        str(source),
        str(target),
        int(asset["size_bytes"]),
        asset.get("sha256"),
    )
    if move["status"] == "moved" and target.is_file():
        _verify(target, asset["size_bytes"], asset.get("sha256"))
        database.complete_move(move["id"])
        return {"asset_id": asset["id"], "source": str(source), "target": str(target)}

    # A worker may have been interrupted after rename and before the DB update.
    # Recognize that exact durable outcome and finish the journal entry.
    if not source.exists() and target.exists():
        _verify(target, asset["size_bytes"], asset.get("sha256"))
        database.complete_move(move["id"])
        return {"asset_id": asset["id"], "source": str(source), "target": str(target)}
    if source == target:
        _verify(target, asset["size_bytes"], asset.get("sha256"))
        database.complete_move(move["id"])
        return {"asset_id": asset["id"], "source": str(source), "target": str(target)}
    if not source.is_file():
        raise OrganizationError(f"Asset source is missing: {source}")
    if target.exists():
        raise OrganizationError(f"Target already exists: {target}")

    database.start_move(move["id"])
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        # Organization is deliberately restricted to an atomic rename. The
        # staging and library roots must therefore share a filesystem.
        if source.stat().st_dev != target.parent.stat().st_dev:
            raise OrganizationError(
                "The staging and library roots must be on the same filesystem "
                "for a true move"
            )
        source.replace(target)
        _verify(target, asset["size_bytes"], asset.get("sha256"))
        database.complete_move(move["id"])
    except Exception as exc:
        database.fail_move(move["id"], str(exc))
        raise
    return {"asset_id": asset["id"], "source": str(source), "target": str(target)}


def organize_assets(config: Config, database: Database, job_id: str) -> dict[str, Any]:
    """Move every fully assigned episode into the in-place library tree.

    The operation journal is written before each rename and the asset path is
    updated only after the target has been verified. Re-running this function
    is therefore safe after a worker restart or a partially completed batch.
    """

    job = database.get_job(job_id)
    if job is None:
        raise KeyError(job_id)
    if job.get("resolved_media_type") != "tv" or not all(
        job.get(field) not in (None, "")
        for field in ("show_provider", "show_id", "show_name", "season")
    ):
        database.audit(
            "organization.deferred",
            {"reason": "show information or season is incomplete"},
            job_id,
            "worker",
        )
        return {"moved": 0, "deferred": True}

    assets = database.list_assets(job_id)
    assignments = {item["asset_id"]: item for item in database.list_assignments(job_id)}
    candidates = [
        asset
        for asset in assets
        if asset["disposition"] == "episode"
        and assignments.get(asset["id"], {}).get("episode_start") is not None
    ]
    if not candidates:
        database.audit(
            "organization.deferred",
            {"reason": "no fully assigned episode assets"},
            job_id,
            "worker",
        )
        return {"moved": 0, "deferred": True}

    if job["state"] != "organizing":
        database.transition(job_id, "organizing", actor="worker")
    groups: dict[tuple[int, int], list[dict[str, Any]]] = defaultdict(list)
    for asset in candidates:
        assignment = assignments[asset["id"]]
        season = int(assignment.get("season") or job["season"])
        if season != int(job["season"]):
            raise OrganizationError(
                f"Asset {asset['path']} uses season {season}, expected {job['season']}"
            )
        groups[(season, int(assignment["episode_start"]))].append(asset)

    moved = []
    try:
        for asset in candidates:
            moved.append(
                _move_one(
                    config,
                    database,
                    job_id,
                    asset,
                    assignments[asset["id"]],
                    groups[(
                        int(assignments[asset["id"]].get("season") or job["season"]),
                        int(assignments[asset["id"]]["episode_start"]),
                    )],
                )
            )
    except Exception as exc:
        database.audit(
            "organization.failed",
            {"moved": moved, "error": str(exc)},
            job_id,
            "worker",
        )
        raise

    database.transition(job_id, "complete", actor="worker")
    database.audit(
        "organization.completed",
        {"moved": moved},
        job_id,
        "worker",
    )
    return {"moved": len(moved), "operations": moved}


# Friendly alias for callers that describe this as organizing a job.
organize_job = organize_assets
