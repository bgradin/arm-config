from __future__ import annotations

from collections import Counter
from statistics import median
from typing import Any, Iterable, Protocol

from ..domain import Suggestion


class OrderContext(Protocol):
    manifest: dict[str, Any]
    sources: list[dict[str, Any]]
    assets: list[dict[str, Any]]


class OrderStrategy:
    name = "base_order_strategy"
    version = "1.0.0"

    def infer_order(self, context: OrderContext) -> list[Suggestion]:
        raise NotImplementedError


def episode_cluster(sources: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return the largest compact runtime cluster without trusting title order."""

    candidates = [
        source
        for source in sources
        if source.get("duration_seconds") is not None
        and 5 * 60 <= float(source["duration_seconds"]) <= 95 * 60
    ]
    if len(candidates) < 2:
        return candidates
    # Identical effective source topology is one content candidate until the
    # relationship analyzer/user resolves whether it is a duplicate/variant.
    unique: dict[str, dict[str, Any]] = {}
    for source in candidates:
        unique.setdefault(source.get("topology_hash") or source["id"], source)
    candidates = list(unique.values())
    clusters = []
    for seed in candidates:
        duration = float(seed["duration_seconds"])
        tolerance = max(90.0, duration * 0.12)
        cluster = [
            item
            for item in candidates
            if abs(float(item["duration_seconds"]) - duration) <= tolerance
        ]
        durations = [float(item["duration_seconds"]) for item in cluster]
        spread = max(durations) - min(durations)
        clusters.append((len(cluster), -spread, cluster))
    return max(clusters, key=lambda item: (item[0], item[1]))[2]


def asset_mapping_issues(
    sources: list[dict[str, Any]], assets: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    mapped: dict[str, list[dict[str, Any]]] = {}
    for asset in assets:
        source_id = asset.get("source_title_id")
        if source_id:
            mapped.setdefault(str(source_id), []).append(asset)
    issues = []
    for source in sources:
        source_assets = mapped.get(source["id"], [])
        if len(source_assets) != 1:
            issues.append(
                {
                    "source_id": source["id"],
                    "source_key": source["source_key"],
                    "asset_count": len(source_assets),
                }
            )
            continue
        asset = source_assets[0]
        mapping = asset.get("metadata", {}).get("source_mapping", {})
        if mapping.get("method") != "ripper_identity":
            issues.append(
                {
                    "source_id": source["id"],
                    "source_key": source["source_key"],
                    "reason": "primary_ripper_identity_missing",
                }
            )
        asset_duration = asset.get("duration_seconds")
        source_duration = source.get("duration_seconds")
        if asset_duration is None or source_duration is None:
            issues.append(
                {
                    "source_id": source["id"],
                    "source_key": source["source_key"],
                    "reason": "duration_validation_missing",
                }
            )
        elif abs(float(asset_duration) - float(source_duration)) > 2.0:
            issues.append(
                {
                    "source_id": source["id"],
                    "source_key": source["source_key"],
                    "reason": "duration_mismatch",
                    "source_seconds": source_duration,
                    "asset_seconds": asset_duration,
                }
            )

        ripper_title = source.get("payload", {}).get("ripper_title", {})
        try:
            source_chapters = int(ripper_title["chapter_count"])
        except (KeyError, TypeError, ValueError):
            source_chapters = None
        asset_chapters = asset.get("metadata", {}).get("chapters")
        if source_chapters is None or not isinstance(asset_chapters, list):
            issues.append(
                {
                    "source_id": source["id"],
                    "source_key": source["source_key"],
                    "reason": "chapter_validation_missing",
                }
            )
        elif source_chapters != len(asset_chapters):
            issues.append(
                {
                    "source_id": source["id"],
                    "source_key": source["source_key"],
                    "reason": "chapter_count_mismatch",
                    "source_count": source_chapters,
                    "asset_count": len(asset_chapters),
                }
            )

        source_streams = Counter(
            str(item.get("type", "")).casefold().rstrip("s")
            for item in ripper_title.get("streams", [])
            if item.get("type")
        )
        asset_streams = Counter(
            str(item.get("codec_type", "")).casefold().rstrip("s")
            for item in asset.get("metadata", {}).get("streams", [])
            if item.get("codec_type")
        )
        if not source_streams or not asset_streams:
            issues.append(
                {
                    "source_id": source["id"],
                    "source_key": source["source_key"],
                    "reason": "stream_validation_missing",
                }
            )
        elif source_streams != asset_streams:
            issues.append(
                {
                    "source_id": source["id"],
                    "source_key": source["source_key"],
                    "reason": "stream_layout_mismatch",
                    "source_streams": dict(source_streams),
                    "asset_streams": dict(asset_streams),
                }
            )
    return issues


def topology_overlap(first: dict[str, Any], second: dict[str, Any]) -> float:
    first_items = {
        (item.get("clip_id"), item.get("in"), item.get("out"))
        for item in first.get("payload", {}).get("topology", [])
    }
    second_items = {
        (item.get("clip_id"), item.get("in"), item.get("out"))
        for item in second.get("payload", {}).get("topology", [])
    }
    if not first_items or not second_items:
        return 0.0
    return len(first_items & second_items) / len(first_items | second_items)


def content_relationship_issues(
    sources: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    issues = []
    for index, first in enumerate(sources):
        for second in sources[index + 1 :]:
            same_topology = (
                first.get("topology_hash")
                and first.get("topology_hash") == second.get("topology_hash")
            )
            overlap = topology_overlap(first, second)
            if not same_topology and overlap < 0.5:
                continue
            issues.append(
                {
                    "first": first["source_key"],
                    "second": second["source_key"],
                    "same_topology": bool(same_topology),
                    "shared_topology_ratio": overlap,
                }
            )
    return issues


def runtime_summary(sources: list[dict[str, Any]]) -> dict[str, Any]:
    durations = [float(item["duration_seconds"]) for item in sources]
    return {
        "count": len(sources),
        "median_seconds": median(durations) if durations else None,
        "durations_seconds": durations,
    }