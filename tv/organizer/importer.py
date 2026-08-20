from __future__ import annotations

import json
import uuid
from pathlib import Path

from .config import Config
from .db import Database
from .normalize import map_assets_to_sources, normalize_sources
from .probe import build_asset
from .util import ensure_within, hash_file, hash_json, media_files


class ImportError(RuntimeError):
    pass


def load_manifest(path: Path) -> dict:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ImportError(f"Cannot read manifest {path}: {exc}") from exc
    if manifest.get("schema_version") != 1:
        raise ImportError(
            f"Unsupported manifest schema: {manifest.get('schema_version')}"
        )
    if not manifest.get("disc", {}).get("fingerprint"):
        raise ImportError("Manifest has no disc fingerprint")
    capture_root = path.resolve().parent
    for item in manifest.get("files", []):
        try:
            captured = ensure_within(capture_root / item["path"], capture_root)
        except (KeyError, TypeError, ValueError) as exc:
            raise ImportError(f"Manifest contains an unsafe captured path: {item}") from exc
        if not captured.is_file():
            raise ImportError(f"Captured input is missing: {captured}")
        if captured.stat().st_size != int(item.get("size", -1)):
            raise ImportError(f"Captured input size changed: {captured}")
        if hash_file(captured) != item.get("sha256"):
            raise ImportError(f"Captured input hash changed: {captured}")
    return manifest


def import_manifest(
    config: Config,
    database: Database,
    manifest_path: Path,
    *,
    rip_root: Path | None = None,
    enqueue_analysis: bool = True,
) -> str:
    job_id = register_manifest(
        database,
        manifest_path,
        rip_root=rip_root,
        state="awaiting_assets",
    )
    if rip_root:
        attach_assets(
            config,
            database,
            job_id,
            rip_root,
            enqueue_analysis=enqueue_analysis,
        )
    elif enqueue_analysis:
        database.transition(job_id, "analyzing")
        database.enqueue("analyze", job_id)
    return job_id


def register_manifest(
    database: Database,
    manifest_path: Path,
    *,
    rip_root: Path | None = None,
    state: str = "ripping",
) -> str:
    manifest_path = manifest_path.resolve()
    rip_root = rip_root.resolve() if rip_root else None
    manifest = load_manifest(manifest_path)
    manifest_hash = hash_json(manifest)
    arm_job_id = manifest.get("capture", {}).get("arm_job_id")
    existing = database.find_existing_job(
        arm_job_id=str(arm_job_id) if arm_job_id is not None else None,
        manifest_path=manifest_path,
        manifest_hash=manifest_hash,
        rip_root=rip_root,
    )
    if existing:
        database.audit(
            "job.import_idempotent",
            {"manifest_path": str(manifest_path)},
            existing["id"],
        )
        return str(existing["id"])
    sources = normalize_sources(manifest)
    job_id = database.create_job(
        disc_fingerprint=manifest["disc"]["fingerprint"],
        disc_type=manifest["disc"]["type"],
        manifest_path=manifest_path,
        manifest_hash=manifest_hash,
        state=state,
        arm_job_id=str(arm_job_id) if arm_job_id is not None else None,
        rip_root=rip_root,
    )
    for source in sources:
        source.setdefault("payload", {})["structural_id"] = source["id"]
        source["id"] = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"{job_id}:{source['source_key']}",
            )
        )
    database.replace_sources(job_id, sources)
    return job_id


def attach_assets(
    config: Config,
    database: Database,
    job_id: str,
    rip_root: Path,
    *,
    enqueue_analysis: bool = True,
) -> None:
    job = database.get_job(job_id)
    if job is None:
        raise KeyError(job_id)
    rip_root = rip_root.resolve()
    if (
        job.get("rip_root") == str(rip_root)
        and job["state"] not in {"capturing", "ripping", "awaiting_assets"}
        and database.list_assets(job_id)
    ):
        database.audit(
            "assets.attach_idempotent",
            {"rip_root": str(rip_root)},
            job_id,
        )
        return
    database.set_rip_root(job_id, rip_root)
    if job["state"] in {"capturing", "ripping"}:
        database.transition(job_id, "awaiting_assets")
    elif job["state"] != "awaiting_assets":
        raise ImportError(f"Cannot attach assets while job is {job['state']}")
    manifest = load_manifest(Path(job["manifest_path"]))
    sources = database.list_sources(job_id)
    paths = media_files(rip_root, [config.library_root])
    assets = [build_asset(path, full_hash=config.hash_assets) for path in paths]
    for asset in assets:
        asset["id"] = str(
            uuid.uuid5(uuid.NAMESPACE_URL, f"{job_id}:{asset['path']}")
        )
    database.replace_assets(
        job_id,
        map_assets_to_sources(assets, sources, manifest),
    )
    database.transition(job_id, "analyzing")
    if enqueue_analysis:
        database.enqueue("analyze", job_id)