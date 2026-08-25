from __future__ import annotations

from statistics import median
from typing import Any, Iterable, Protocol

from ..domain import Suggestion


class OrderContext(Protocol):
    manifest: dict[str, Any]
    sources: list[dict[str, Any]]


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
