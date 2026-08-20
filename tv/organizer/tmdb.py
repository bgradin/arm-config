from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .db import Database
from .util import hash_json


class TMDBError(RuntimeError):
    pass


class TMDBClient:
    base_url = "https://api.themoviedb.org/3"

    def __init__(self, token: str, database: Database):
        self.token = token
        self.database = database

    def _get(self, path: str, parameters: dict[str, Any] | None = None) -> dict:
        if not self.token:
            raise TMDBError("TMDB_READ_TOKEN is not configured")
        parameters = parameters or {}
        key = hash_json({"path": path, "parameters": parameters})
        cached = self.database.cache_get(key)
        if cached is not None:
            return cached
        url = f"{self.base_url}{path}"
        if parameters:
            url += "?" + urllib.parse.urlencode(parameters)
        request = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/json",
                "User-Agent": "arm-tv/0.1.0",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                result = json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                raise TMDBError("TMDB rate limit reached; retry later") from exc
            raise TMDBError(f"TMDB returned HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise TMDBError(f"TMDB request failed: {exc}") from exc
        self.database.cache_put(key, result)
        return result

    def search_tv(
        self,
        query: str,
        *,
        year: int | None = None,
        language: str = "en-US",
    ) -> dict:
        parameters: dict[str, Any] = {"query": query, "language": language}
        if year:
            parameters["first_air_date_year"] = year
        return self._get("/search/tv", parameters)

    def series(self, series_id: str, language: str = "en-US") -> dict:
        return self._get(f"/tv/{series_id}", {"language": language})

    def season(
        self, series_id: str, season_number: int, language: str = "en-US"
    ) -> dict:
        return self._get(
            f"/tv/{series_id}/season/{season_number}",
            {"language": language},
        )

    def episode_groups(self, series_id: str) -> dict:
        return self._get(f"/tv/{series_id}/episode_groups")

    def episode_group(self, group_id: str) -> dict:
        return self._get(f"/tv/episode_group/{group_id}")
