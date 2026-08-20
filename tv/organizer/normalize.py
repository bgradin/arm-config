from __future__ import annotations

import re
import uuid
from pathlib import Path
from typing import Any

from .makemkv import duration_seconds
from .util import hash_json


PLAY_OPERATIONS = {
    "play_playlist",
    "play_playlist_at_play_item",
    "play_playlist_at_mark",
}


def _stream_summary(play_item: dict[str, Any]) -> list[dict[str, Any]]:
    streams = []
    table = play_item.get("stn_table", {}).get("streams", {})
    for category, items in sorted(table.items()):
        for item in items:
            streams.append(
                {
                    "category": category,
                    "codec": item.get("codec"),
                    "language": item.get("language"),
                    "video_format": item.get("video_format"),
                    "frame_rate": item.get("frame_rate"),
                    "audio_format": item.get("audio_format"),
                }
            )
    return streams


def _index_maps(navigation: dict[str, Any]) -> tuple[dict[int, int], dict[int, dict[str, Any]]]:
    index = navigation.get("index", {})
    movie = navigation.get("movie_object", {})
    title_objects: dict[int, int] = {}
    for title in index.get("indexes", {}).get("titles", []):
        if title.get("object_type", {}).get("name") != "HDMV":
            continue
        title_objects[int(title["title_number"])] = int(
            title.get("object", {}).get("id_ref", -1)
        )
    objects = {
        int(item["object_number"]): item
        for item in movie.get("movie_objects", {}).get("objects", [])
    }
    return title_objects, objects


def normalize_sources(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    fingerprint = manifest["disc"]["fingerprint"]
    navigation = manifest.get("navigation", {})
    sources: dict[str, dict[str, Any]] = {}
    title_objects, objects = _index_maps(navigation)
    object_titles = {object_id: title for title, object_id in title_objects.items()}

    for playlist_id, playlist in sorted(navigation.get("playlists", {}).items()):
        play_items = playlist.get("playlist", {}).get("play_items", [])
        topology = [
            {
                "clip_id": item.get("clip_id"),
                "in": item.get("in_time"),
                "out": item.get("out_time"),
                "angles": [angle.get("clip_id") for angle in item.get("angles", [])],
            }
            for item in play_items
        ]
        chapters = [
            round(float(item["seconds"]), 3)
            for item in (playlist.get("marks") or {}).get("chapters", [])
            if item.get("seconds") is not None
        ]
        streams = []
        for item in play_items:
            streams.extend(_stream_summary(item))
        references = []
        for object_id, movie_object in objects.items():
            operations = movie_object.get("commands", [])
            if any(
                command.get("operation") in PLAY_OPERATIONS
                and int(command.get("dst", -1)) == int(playlist_id)
                for command in operations
            ):
                references.append(
                    {
                        "object_id": object_id,
                        "title_number": object_titles.get(object_id),
                        "commands": operations,
                    }
                )
        key = f"mpls:{int(playlist_id):05d}"
        sources[key] = {
            "id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"{fingerprint}:{key}")),
            "source_key": key,
            "source_type": "bluray_playlist",
            "label": f"Playlist {int(playlist_id):05d}",
            "duration_seconds": playlist.get("playlist", {}).get(
                "duration_seconds"
            ),
            "topology_hash": hash_json(topology) if topology else None,
            "chapter_fingerprint": hash_json(chapters) if chapters else None,
            "stream_fingerprint": hash_json(streams) if streams else None,
            "payload": {
                "playlist_id": int(playlist_id),
                "topology": topology,
                "chapters": chapters,
                "streams": streams,
                "references": references,
                "playlist": playlist,
            },
        }

    for title_id, title in manifest.get("ripper", {}).get("titles", {}).items():
        source_file = str(title.get("source_file", ""))
        match = re.search(r"(\d{5})\.mpls$", source_file, re.IGNORECASE)
        if match:
            key = f"mpls:{int(match.group(1)):05d}"
            if key in sources:
                sources[key]["payload"]["ripper_title"] = title
                continue
        key = f"makemkv:{int(title_id):05d}"
        if key in sources:
            continue
        streams = title.get("streams", [])
        segment_map = title.get("segment_map")
        chapter_count = title.get("chapter_count")
        sources[key] = {
            "id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"{fingerprint}:{key}")),
            "source_key": key,
            "source_type": "ripper_title",
            "label": title.get("name") or f"Ripper title {title_id}",
            "duration_seconds": duration_seconds(title.get("duration")),
            "topology_hash": hash_json(segment_map) if segment_map else None,
            "chapter_fingerprint": hash_json(chapter_count) if chapter_count else None,
            "stream_fingerprint": hash_json(streams) if streams else None,
            "payload": {"ripper_title": title, "topology": []},
        }
    return list(sources.values())


def map_assets_to_sources(
    assets: list[dict[str, Any]],
    sources: list[dict[str, Any]],
    manifest: dict[str, Any],
) -> list[dict[str, Any]]:
    by_key = {source["source_key"]: source for source in sources}
    ripper_titles = manifest.get("ripper", {}).get("titles", {})
    output_names: dict[str, str] = {}
    title_keys: dict[int, str] = {}
    for title_id_text, title in ripper_titles.items():
        title_id = int(title_id_text)
        source_file = str(title.get("source_file", ""))
        match = re.search(r"(\d{5})\.mpls$", source_file, re.IGNORECASE)
        key = (
            f"mpls:{int(match.group(1)):05d}"
            if match
            else f"makemkv:{title_id:05d}"
        )
        if key in by_key:
            title_keys[title_id] = key
        output = title.get("output_file")
        if output:
            output_names[Path(str(output)).name.lower()] = key

    for asset in assets:
        name = Path(asset["path"]).name.lower()
        key = output_names.get(name)
        if not key:
            match = re.search(r"title[_-]?t?(\d+)", name, re.IGNORECASE)
            if match:
                key = title_keys.get(int(match.group(1)))
        if not key and asset.get("duration_seconds") is not None:
            candidates = [
                source
                for source in sources
                if source.get("duration_seconds") is not None
                and abs(
                    float(source["duration_seconds"])
                    - float(asset["duration_seconds"])
                )
                <= 2.0
            ]
            if len(candidates) == 1:
                key = candidates[0]["source_key"]
                asset.setdefault("metadata", {})["source_mapping"] = {
                    "method": "unique_duration",
                    "confidence": 0.7,
                }
        if key and key in by_key:
            asset["source_title_id"] = by_key[key]["id"]
            asset.setdefault("metadata", {}).setdefault(
                "source_mapping",
                {"method": "ripper_identity", "confidence": 0.98},
            )
        else:
            asset["source_title_id"] = None
            asset.setdefault("metadata", {})["source_mapping"] = {
                "method": "unresolved",
                "confidence": 0.0,
            }
    return assets