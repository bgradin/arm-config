from __future__ import annotations

import json
from pathlib import Path

from ..config import Config
from ..db import Database
from ..util import hash_file, hash_json, quick_file_fingerprint


def config_for(root: Path, *, versions: bool = False) -> Config:
    config = Config(
        data_root=root / "data",
        inbox_root=root / "inbox",
        library_root=root / "library",
        worker_enabled=False,
        jellyfin_episode_versions=versions,
    )
    config.ensure_directories()
    return config


def database_for(config: Config) -> Database:
    database = Database(config.database_path)
    database.initialize()
    return database


def review_job(config: Config, database: Database, assets: list[tuple[str, bytes]]) -> str:
    manifest = {
        "schema_version": 1,
        "disc": {"fingerprint": "f" * 64, "type": "bluray"},
        "navigation": {},
        "ripper": {"titles": {}},
    }
    manifest_path = config.data_root / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    job_id = database.create_job(
        disc_fingerprint=manifest["disc"]["fingerprint"],
        disc_type="bluray",
        manifest_path=manifest_path,
        manifest_hash=hash_json(manifest),
        state="needs_review",
        rip_root=config.inbox_root,
    )
    records = []
    for index, (name, content) in enumerate(assets, start=1):
        path = config.inbox_root / name
        path.write_bytes(content)
        records.append(
            {
                "id": f"asset-{index}",
                "path": str(path),
                "size_bytes": path.stat().st_size,
                "sha256": hash_file(path),
                "quick_fingerprint": quick_file_fingerprint(path),
                "duration_seconds": 1320.0,
                "chapter_fingerprint": "chapters",
                "stream_fingerprint": "streams",
                "metadata": {},
            }
        )
    database.replace_assets(job_id, records)
    database.resolve_job(
        job_id,
        {
            "resolved_media_type": "tv",
            "show_provider": "tmdb",
            "show_id": "123",
            "show_name": "Example Show",
            "show_year": 2020,
            "season": 1,
        },
    )
    return job_id

