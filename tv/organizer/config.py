from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _bool_env(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Config:
    data_root: Path
    inbox_root: Path
    library_root: Path
    bind: str = "0.0.0.0"
    port: int = 8081
    tmdb_token: str = ""
    auth_token: str = ""
    worker_enabled: bool = True
    poll_seconds: float = 1.0
    jellyfin_episode_versions: bool = False
    hash_assets: bool = True

    @property
    def database_path(self) -> Path:
        return self.data_root / "organizer.sqlite3"

    @property
    def captures_root(self) -> Path:
        return self.data_root / "captures"

    @property
    def plans_root(self) -> Path:
        return self.data_root / "plans"

    def ensure_directories(self) -> None:
        for path in (
            self.data_root,
            self.captures_root,
            self.plans_root,
            self.inbox_root,
            self.library_root,
        ):
            path.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_env(cls) -> "Config":
        data_root = Path(
            os.environ.get("TV_DATA", "./data/organizer")
        ).expanduser()
        inbox_root = Path(
            os.environ.get("TV_INBOX", "./inbox")
        ).expanduser()
        library_root = Path(
            os.environ.get("TV_LIBRARY", str(inbox_root))
        ).expanduser()
        return cls(
            data_root=data_root.resolve(),
            inbox_root=inbox_root.resolve(),
            library_root=library_root.resolve(),
            bind=os.environ.get("TV_BIND", "0.0.0.0"),
            port=int(os.environ.get("TV_PORT", "8081")),
            tmdb_token=os.environ.get("TMDB_READ_TOKEN", ""),
            auth_token=os.environ.get("TV_AUTH_TOKEN", ""),
            worker_enabled=_bool_env("TV_WORKER", True),
            poll_seconds=float(
                os.environ.get("TV_POLL_SECONDS", "1")
            ),
            jellyfin_episode_versions=_bool_env(
                "JELLYFIN_EPISODE_VERSIONS", False
            ),
            hash_assets=_bool_env("TV_HASH_ASSETS", True),
        )
