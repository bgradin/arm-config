from __future__ import annotations

from collections import defaultdict
from typing import Any

from .config import Config
from .db import Database
from .util import hash_json


def approval_fingerprint(database: Database, job_id: str) -> str:
    job = database.get_job(job_id)
    if job is None:
        raise KeyError(job_id)
    identity = {
        key: job.get(key)
        for key in (
            "resolved_media_type",
            "show_provider",
            "show_id",
            "show_name",
            "show_year",
            "season",
            "manifest_hash",
        )
    }
    assets = [
        {
            key: asset.get(key)
            for key in (
                "id",
                "path",
                "size_bytes",
                "sha256",
                "disposition",
                "edition_name",
                "preferred",
            )
        }
        for asset in database.list_assets(job_id)
    ]
    assignments = sorted(
        database.list_assignments(job_id), key=lambda item: item["asset_id"]
    )
    for assignment in assignments:
        assignment.pop("id", None)
        assignment.pop("updated_at", None)
    return hash_json(
        {"identity": identity, "assets": assets, "assignments": assignments}
    )


def approval_errors(
    config: Config, database: Database, job_id: str
) -> list[str]:
    job = database.get_job(job_id)
    if job is None:
        return ["Job does not exist."]
    errors = []
    if job.get("resolved_media_type") != "tv":
        errors.append("Media type must be resolved as TV.")
    if not all(
        job.get(field)
        for field in ("show_provider", "show_id", "show_name")
    ):
        errors.append("A provider-backed show identity is required.")
    if job.get("season") is None:
        errors.append("A season is required.")

    assets = database.list_assets(job_id)
    assignments = {
        item["asset_id"]: item for item in database.list_assignments(job_id)
    }
    if not assets:
        errors.append("The job has no ripped assets.")
    for asset in assets:
        disposition = asset["disposition"]
        if disposition == "unresolved":
            errors.append(f"Asset {asset['path']} has no disposition.")
        if disposition == "episode":
            assignment = assignments.get(asset["id"])
            if not assignment or assignment.get("episode_start") is None:
                errors.append(f"Asset {asset['path']} has no episode assignment.")
            elif int(assignment.get("season") or job.get("season")) != int(
                job["season"]
            ):
                errors.append(f"Asset {asset['path']} uses a different season.")

    episode_groups: dict[tuple[int, int], list[dict[str, Any]]] = defaultdict(list)
    for asset in assets:
        if asset["disposition"] != "episode":
            continue
        assignment = assignments.get(asset["id"])
        if not assignment or assignment.get("episode_start") is None:
            continue
        key = (
            int(assignment.get("season") or job["season"]),
            int(assignment["episode_start"]),
        )
        episode_groups[key].append(asset)
    for key, group in episode_groups.items():
        if len(group) < 2:
            continue
        group_assignments = [assignments[item["id"]] for item in group]
        parts = [item.get("part") for item in group_assignments]
        if all(part is not None for part in parts) and len(set(parts)) == len(parts):
            continue
        if len({asset.get("edition_name") for asset in group}) != len(group):
            errors.append(
                f"Episode S{key[0]:02d}E{key[1]:02d} has duplicate edition labels."
            )
        if not config.jellyfin_episode_versions:
            preferred = [asset for asset in group if asset["preferred"]]
            if len(preferred) != 1:
                errors.append(
                    f"Episode S{key[0]:02d}E{key[1]:02d} needs exactly one "
                    "preferred edition for this Jellyfin profile."
                )

    pending_relationships = [
        suggestion
        for suggestion in database.list_suggestions(job_id)
        if suggestion["kind"] in {
            "duplicate_group",
            "edition_group",
            "stream_variant_group",
        }
        and suggestion["status"] == "pending"
        and suggestion["confidence"] >= 0.65
    ]
    if pending_relationships:
        errors.append(
            "Probable duplicate or edition relationships must be accepted or rejected."
        )
    return errors


def approve_job(config: Config, database: Database, job_id: str) -> None:
    errors = approval_errors(config, database, job_id)
    if errors:
        raise ValueError("\n".join(errors))
    job = database.get_job(job_id)
    assert job is not None
    plan = database.latest_plan(job_id)
    if not plan or plan["status"] != "draft":
        raise ValueError("Create a valid dry-run plan before approval.")
    if plan["plan"].get("approval_fingerprint") != approval_fingerprint(
        database, job_id
    ):
        raise ValueError("Review decisions changed after the dry-run plan; create a new plan.")
    if plan["plan"].get("manifest_hash") != job["manifest_hash"]:
        raise ValueError("The dry-run plan uses a different manifest revision.")
    database.transition(job_id, "approved", actor="user")