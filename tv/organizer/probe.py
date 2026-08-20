from __future__ import annotations

import json
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any

from .util import hash_file, hash_json, quick_file_fingerprint


def probe_media(path: Path) -> dict[str, Any]:
    executable = shutil.which("ffprobe")
    if not executable:
        return {"available": False, "reason": "ffprobe_not_installed"}
    command = [
        executable,
        "-v",
        "error",
        "-show_format",
        "-show_streams",
        "-show_chapters",
        "-of",
        "json",
        str(path),
    ]
    try:
        completed = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=120,
        )
        return {"available": True, **json.loads(completed.stdout)}
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError) as exc:
        return {
            "available": False,
            "reason": "ffprobe_failed",
            "error": str(exc),
        }


def _duration(metadata: dict[str, Any]) -> float | None:
    try:
        return float(metadata.get("format", {}).get("duration"))
    except (TypeError, ValueError):
        pass
    durations = []
    for stream in metadata.get("streams", []):
        try:
            durations.append(float(stream["duration"]))
        except (KeyError, TypeError, ValueError):
            continue
    return max(durations) if durations else None


def _chapter_fingerprint(metadata: dict[str, Any]) -> str | None:
    chapters = []
    for chapter in metadata.get("chapters", []):
        try:
            chapters.append(round(float(chapter["start_time"]), 3))
        except (KeyError, TypeError, ValueError):
            continue
    return hash_json(chapters) if chapters else None


def _stream_fingerprint(metadata: dict[str, Any]) -> str | None:
    streams = []
    for stream in metadata.get("streams", []):
        streams.append(
            {
                "type": stream.get("codec_type"),
                "codec": stream.get("codec_name"),
                "profile": stream.get("profile"),
                "width": stream.get("width"),
                "height": stream.get("height"),
                "channels": stream.get("channels"),
                "language": stream.get("tags", {}).get("language"),
                "title": stream.get("tags", {}).get("title"),
            }
        )
    return hash_json(streams) if streams else None


def build_asset(path: Path, *, full_hash: bool) -> dict[str, Any]:
    metadata = probe_media(path)
    return {
        "id": str(uuid.uuid5(uuid.NAMESPACE_URL, str(path.resolve()))),
        "path": str(path.resolve()),
        "size_bytes": path.stat().st_size,
        "sha256": hash_file(path) if full_hash else None,
        "quick_fingerprint": quick_file_fingerprint(path),
        "duration_seconds": _duration(metadata),
        "chapter_fingerprint": _chapter_fingerprint(metadata),
        "stream_fingerprint": _stream_fingerprint(metadata),
        "metadata": metadata,
    }
