from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


MEDIA_EXTENSIONS = {
    ".avi",
    ".m2ts",
    ".mkv",
    ".mp4",
    ".ts",
    ".vob",
    ".webm",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def hash_json(value: Any) -> str:
    return hash_bytes(canonical_json(value).encode("utf-8"))


def hash_file(path: Path, chunk_size: int = 4 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def quick_file_fingerprint(path: Path) -> str:
    """Hash size plus samples without claiming byte identity."""
    stat = path.stat()
    digest = hashlib.sha256()
    digest.update(str(stat.st_size).encode("ascii"))
    sample_size = 1024 * 1024
    with path.open("rb") as handle:
        digest.update(handle.read(sample_size))
        if stat.st_size > sample_size:
            handle.seek(max(0, stat.st_size - sample_size))
            digest.update(handle.read(sample_size))
    return digest.hexdigest()


def media_files(root: Path, excluded_roots: Iterable[Path] = ()) -> list[Path]:
    excluded = [path.resolve() for path in excluded_roots]
    result: list[Path] = []
    if not root.exists():
        return result
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in MEDIA_EXTENSIONS:
            continue
        resolved = path.resolve()
        if any(resolved == base or base in resolved.parents for base in excluded):
            continue
        result.append(resolved)
    return sorted(result)


_UNSAFE_COMPONENT = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_SPACE_RUN = re.compile(r"\s+")


def safe_component(value: str, fallback: str = "Unknown") -> str:
    value = _UNSAFE_COMPONENT.sub(" ", value)
    value = _SPACE_RUN.sub(" ", value).strip(" .")
    return value or fallback


def ensure_within(path: Path, root: Path) -> Path:
    resolved = path.resolve()
    resolved_root = root.resolve()
    if resolved != resolved_root and resolved_root not in resolved.parents:
        raise ValueError(f"Path escapes configured root: {resolved}")
    return resolved


def atomic_json_write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    text = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
    with temporary.open("w", encoding="utf-8") as handle:
        handle.write(text)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
