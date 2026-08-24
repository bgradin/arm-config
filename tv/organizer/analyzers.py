from __future__ import annotations

import math
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from statistics import median
from typing import Any, Iterable

from .config import Config
from .db import Database
from .domain import Evidence, Suggestion
from .importer import load_manifest
from .util import utc_now
from .order_strategies import DEFAULT_ORDER_STRATEGIES
from .order_strategies.base import OrderStrategy, episode_cluster


@dataclass
class AnalysisContext:
    config: Config
    database: Database
    job: dict[str, Any]
    manifest: dict[str, Any]
    sources: list[dict[str, Any]]
    assets: list[dict[str, Any]]


class Analyzer:
    name = "base"
    version = "1.0.0"
    output_kinds: frozenset[str] = frozenset()

    def analyze(self, context: AnalysisContext) -> list[Suggestion]:
        raise NotImplementedError


class ClassificationAnalyzer(Analyzer):
    name = "episode_shape"
    output_kinds = frozenset({"media_type", "episode_candidates"})

    def analyze(self, context: AnalysisContext) -> list[Suggestion]:
        cluster = episode_cluster(context.sources)
        if len(cluster) < 2:
            return [
                Suggestion(
                    kind="media_type",
                    value={"media_type": "unknown"},
                    confidence=0.0,
                    contradictions=[
                        Evidence(
                            "insufficient_episode_cluster",
                            "Fewer than two distinct episode-like titles were found.",
                        )
                    ],
                    analyzer=self.name,
                    analyzer_version=self.version,
                )
            ]
        durations = [float(source["duration_seconds"]) for source in cluster]
        center = median(durations)
        deviation = (
            math.sqrt(sum((item - center) ** 2 for item in durations) / len(durations))
            / center
            if center
            else 1.0
        )
        count_score = {2: 0.62, 3: 0.76}.get(len(cluster), 0.86)
        compact_bonus = 0.06 if deviation <= 0.08 else 0.02
        navigation_bonus = 0.04 if all(
            source.get("payload", {}).get("references") for source in cluster
        ) else 0.0
        confidence = min(0.96, count_score + compact_bonus + navigation_bonus)
        source_ids = [source["id"] for source in cluster]
        evidence = [
            Evidence(
                "repeated_episode_durations",
                f"Found {len(cluster)} distinct titles around {center / 60:.1f} minutes.",
                count_score,
                {"durations_seconds": [round(item, 3) for item in durations]},
            ),
            Evidence(
                "duration_cluster_compactness",
                f"Relative duration deviation is {deviation:.3f}.",
                compact_bonus,
            ),
        ]
        if navigation_bonus:
            evidence.append(
                Evidence(
                    "navigation_references",
                    "Every episode-like playlist is referenced by an HDMV title object.",
                    navigation_bonus,
                )
            )
        return [
            Suggestion(
                kind="media_type",
                value={
                    "media_type": "tv",
                    "episode_source_ids": source_ids,
                    "median_duration_seconds": center,
                },
                confidence=confidence,
                evidence=evidence,
                analyzer=self.name,
                analyzer_version=self.version,
            ),
            Suggestion(
                kind="episode_candidates",
                value=source_ids,
                confidence=confidence,
                evidence=evidence,
                analyzer=self.name,
                analyzer_version=self.version,
            ),
        ]


class DiscIdentityAnalyzer(Analyzer):
    name = "disc_identity"
    output_kinds = frozenset({"show_name", "season"})

    _season = re.compile(r"(?:season|series|\bs)\s*[-_. ]?(\d{1,2})\b", re.I)

    def analyze(self, context: AnalysisContext) -> list[Suggestion]:
        signals: dict[str, set[str]] = defaultdict(set)
        disc = context.manifest.get("disc", {})
        for value in disc.get("descriptive_titles", []):
            if value:
                signals[str(value)].add("bluray_metadata")
        volume = context.manifest.get("ripper", {}).get("disc", {}).get(
            "volume_name"
        )
        if volume:
            signals[str(volume)].add("makemkv_volume")
        label = disc.get("volume_label")
        if label and label not in {"BDMV", "VIDEO_TS"}:
            signals[str(label)].add("filesystem_label")

        suggestions = []
        ranked = sorted(
            signals.items(),
            key=lambda item: (len(item[1]), "bluray_metadata" in item[1]),
            reverse=True,
        )
        if ranked:
            name, origins = ranked[0]
            confidence = 0.88 if len(origins) >= 2 else (
                0.72 if "bluray_metadata" in origins else 0.42
            )
            suggestions.append(
                Suggestion(
                    kind="show_name",
                    value={"name": name, "signals": sorted(origins)},
                    confidence=confidence,
                    evidence=[
                        Evidence(
                            "disc_text_signal",
                            f"Disc text candidate {name!r} came from {', '.join(sorted(origins))}.",
                            confidence,
                        )
                    ],
                    analyzer=self.name,
                    analyzer_version=self.version,
                )
            )
        season_values: dict[int, set[str]] = defaultdict(set)
        for name, origins in signals.items():
            match = self._season.search(name.replace("_", " "))
            if match:
                season_values[int(match.group(1))].update(origins)
        for season, origins in sorted(season_values.items()):
            suggestions.append(
                Suggestion(
                    kind="season",
                    value={"season": season},
                    confidence=0.8 if "bluray_metadata" in origins else 0.55,
                    evidence=[
                        Evidence(
                            "explicit_season_text",
                            f"Season {season} appears in captured disc text.",
                            0.55,
                            {"origins": sorted(origins)},
                        )
                    ],
                    analyzer=self.name,
                    analyzer_version=self.version,
                )
            )
        return suggestions


class OrderAnalyzer(Analyzer):
    """Adapter keeping order inference behind independently replaceable modules."""

    name = "order_strategies"
    output_kinds = frozenset({"episode_order"})

    def __init__(
        self,
        strategies: Iterable[OrderStrategy] = DEFAULT_ORDER_STRATEGIES,
    ) -> None:
        self.strategies = tuple(strategies)

    def analyze(self, context: AnalysisContext) -> list[Suggestion]:
        suggestions: list[Suggestion] = []
        for strategy in self.strategies:
            suggestions.extend(strategy.infer_order(context))
        return suggestions


def _shared_topology(first: dict[str, Any], second: dict[str, Any]) -> float:
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


def _topology_differences(
    first: dict[str, Any], second: dict[str, Any]
) -> dict[str, Any]:
    def normalized(source: dict[str, Any]) -> list[tuple[Any, Any, Any]]:
        return [
            (item.get("clip_id"), item.get("in"), item.get("out"))
            for item in source.get("payload", {}).get("topology", [])
        ]

    first_items = normalized(first)
    second_items = normalized(second)
    first_set = set(first_items)
    second_set = set(second_items)
    return {
        "shared_segments": [list(item) for item in first_items if item in second_set],
        "first_unique_segments": [list(item) for item in first_items if item not in second_set],
        "second_unique_segments": [list(item) for item in second_items if item not in first_set],
        "chapter_fingerprints": [
            first.get("chapter_fingerprint"),
            second.get("chapter_fingerprint"),
        ],
        "stream_fingerprints": [
            first.get("stream_fingerprint"),
            second.get("stream_fingerprint"),
        ],
        "labels": [first.get("label"), second.get("label")],
    }


class DuplicateEditionAnalyzer(Analyzer):
    name = "content_relationships"
    output_kinds = frozenset({"duplicate_group", "stream_variant_group", "edition_group"})

    def analyze(self, context: AnalysisContext) -> list[Suggestion]:
        suggestions: list[Suggestion] = []
        by_hash: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for asset in context.assets:
            if asset.get("sha256"):
                by_hash[asset["sha256"]].append(asset)
        for digest, assets in by_hash.items():
            if len(assets) < 2:
                continue
            suggestions.append(
                Suggestion(
                    kind="duplicate_group",
                    value={
                        "classification": "exact_duplicate",
                        "asset_ids": [asset["id"] for asset in assets],
                        "sha256": digest,
                    },
                    confidence=1.0,
                    evidence=[
                        Evidence(
                            "identical_asset_hash",
                            "The complete rip assets have identical SHA-256 hashes.",
                            1.0,
                        )
                    ],
                    analyzer=self.name,
                    analyzer_version=self.version,
                )
            )

        by_topology: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for source in context.sources:
            if source.get("topology_hash"):
                by_topology[source["topology_hash"]].append(source)
        for topology_hash, sources in by_topology.items():
            if len(sources) < 2:
                continue
            stream_fingerprints = {
                source.get("stream_fingerprint")
                for source in sources
                if source.get("stream_fingerprint")
            }
            stream_variant = len(stream_fingerprints) > 1
            suggestions.append(
                Suggestion(
                    kind="stream_variant_group" if stream_variant else "duplicate_group",
                    value={
                        "classification": (
                            "stream_variant" if stream_variant else "probable_duplicate"
                        ),
                        "source_ids": [source["id"] for source in sources],
                        "source_keys": [source["source_key"] for source in sources],
                        "labels": [source.get("label") for source in sources],
                        "stream_fingerprints": sorted(stream_fingerprints),
                        "topology_hash": topology_hash,
                    },
                    confidence=0.88 if stream_variant else 0.9,
                    evidence=[
                        Evidence(
                            "identical_effective_topology",
                            (
                                "The source titles use identical video topology but different stream layouts."
                                if stream_variant
                                else "The source titles use identical ordered clip/cell ranges."
                            ),
                            0.9,
                        )
                    ],
                    contradictions=[
                        Evidence(
                            (
                                "material_stream_difference"
                                if stream_variant
                                else "asset_content_not_confirmed"
                            ),
                            (
                                "Audio or subtitle selection differs; this is not automatically a duplicate or edition."
                                if stream_variant
                                else "A full aligned asset comparison has not confirmed byte identity."
                            ),
                            -0.1,
                        )
                    ],
                    analyzer=self.name,
                    analyzer_version=self.version,
                )
            )

        for index, first in enumerate(context.sources):
            for second in context.sources[index + 1 :]:
                if first.get("topology_hash") == second.get("topology_hash"):
                    continue
                overlap = _shared_topology(first, second)
                if overlap < 0.5:
                    continue
                first_duration = first.get("duration_seconds") or 0
                second_duration = second.get("duration_seconds") or 0
                suggestions.append(
                    Suggestion(
                        kind="edition_group",
                        value={
                            "classification": "distinct_edition_candidate",
                            "source_ids": [first["id"], second["id"]],
                            "shared_topology_ratio": overlap,
                            "duration_delta_seconds": abs(
                                float(first_duration) - float(second_duration)
                            ),
                            **_topology_differences(first, second),
                        },
                        confidence=min(0.92, 0.55 + overlap * 0.35),
                        evidence=[
                            Evidence(
                                "shared_branch_segments",
                                "The titles share most segments but select at least one different segment.",
                                overlap,
                            )
                        ],
                        contradictions=[
                            Evidence(
                                "edition_label_unconfirmed",
                                "No explicit cut label has been confirmed by a user.",
                                -0.1,
                            )
                        ],
                        analyzer=self.name,
                        analyzer_version=self.version,
                    )
                )

        probable_assets = defaultdict(list)
        for asset in context.assets:
            if asset.get("duration_seconds") is None:
                continue
            key = (
                round(float(asset["duration_seconds"]), 0),
                asset.get("chapter_fingerprint"),
                asset.get("stream_fingerprint"),
            )
            probable_assets[key].append(asset)
        for key, assets in probable_assets.items():
            hashes = {asset.get("sha256") for asset in assets}
            if len(assets) < 2 or len(hashes) <= 1:
                continue
            suggestions.append(
                Suggestion(
                    kind="duplicate_group",
                    value={
                        "classification": "probable_duplicate",
                        "asset_ids": [asset["id"] for asset in assets],
                    },
                    confidence=0.68,
                    evidence=[
                        Evidence(
                            "matching_duration_chapters_streams",
                            "Assets share duration, chapter, and stream fingerprints.",
                            0.68,
                        )
                    ],
                    contradictions=[
                        Evidence(
                            "different_asset_hashes",
                            "The complete file hashes differ, so an editorial difference remains possible.",
                            -0.25,
                        )
                    ],
                    analyzer=self.name,
                    analyzer_version=self.version,
                )
            )
        return suggestions


_EPISODE = re.compile(r"S(?P<season>\d+)E(?P<start>\d+)(?:-?E(?P<end>\d+))?", re.I)


class LibraryContextAnalyzer(Analyzer):
    name = "library_context"
    output_kinds = frozenset({"library_snapshot", "starting_episode"})

    def analyze(self, context: AnalysisContext) -> list[Suggestion]:
        if not context.job.get("show_id") or context.job.get("season") is None:
            return []
        occupied = set()
        files = []
        provider = str(context.job.get("show_provider") or "tmdb").lower()
        identifier = str(context.job["show_id"])
        marker = f"[{provider}id-{identifier}]".casefold()
        show_roots = (
            [
                path
                for path in context.config.library_root.iterdir()
                if path.is_dir() and marker in path.name.casefold()
            ]
            if context.config.library_root.exists()
            else []
        )
        for path in (
            child for root in show_roots for child in root.rglob("*")
        ):
            if not path.is_file():
                continue
            match = _EPISODE.search(path.name)
            if not match or int(match.group("season")) != int(context.job["season"]):
                continue
            start = int(match.group("start"))
            end = int(match.group("end") or start)
            occupied.update(range(start, end + 1))
            stat = path.stat()
            files.append(
                {
                    "path": str(path),
                    "size_bytes": stat.st_size,
                    "mtime_ns": stat.st_mtime_ns,
                    "device": stat.st_dev,
                    "inode": stat.st_ino,
                }
            )
        suggestions = [
            Suggestion(
                kind="library_snapshot",
                value={
                    "provider": provider,
                    "show_id": identifier,
                    "season": int(context.job["season"]),
                    "observed_at": utc_now(),
                    "occupied_episodes": sorted(occupied),
                    "files": files,
                },
                confidence=1.0,
                evidence=[
                    Evidence(
                        "filesystem_inventory",
                        f"Observed {len(files)} matching library files.",
                        1.0,
                    )
                ],
                analyzer=self.name,
                analyzer_version=self.version,
            )
        ]
        if occupied:
            suggestions.append(
                Suggestion(
                    kind="starting_episode",
                    value={"episode": max(occupied) + 1},
                    confidence=0.42,
                    evidence=[
                        Evidence(
                            "next_after_max_existing",
                            "This is the next number after the highest existing episode.",
                            0.42,
                        )
                    ],
                    contradictions=[
                        Evidence(
                            "disc_sequence_not_proven",
                            "Existing files do not prove that this disc follows them.",
                            -0.4,
                        )
                    ],
                    analyzer=self.name,
                    analyzer_version=self.version,
                )
            )
        return suggestions


DEFAULT_ANALYZERS: tuple[Analyzer, ...] = (
    ClassificationAnalyzer(),
    DiscIdentityAnalyzer(),
    OrderAnalyzer(),
    DuplicateEditionAnalyzer(),
    LibraryContextAnalyzer(),
)


def analyze_job(
    config: Config,
    database: Database,
    job_id: str,
    analyzers: Iterable[Analyzer] = DEFAULT_ANALYZERS,
) -> list[dict[str, Any]]:
    job = database.get_job(job_id)
    if job is None:
        raise KeyError(job_id)
    if job["state"] != "analyzing":
        database.transition(job_id, "analyzing")
        job = database.get_job(job_id)
        assert job is not None
    manifest = load_manifest(Path(job["manifest_path"]))
    context = AnalysisContext(
        config=config,
        database=database,
        job=job,
        manifest=manifest,
        sources=database.list_sources(job_id),
        assets=database.list_assets(job_id),
    )
    suggestions = []
    accepted_kinds = {
        item["kind"]
        for item in database.list_suggestions(job_id, include_superseded=True)
        if item["status"] == "accepted"
    }
    try:
        for analyzer in analyzers:
            if analyzer.output_kinds & accepted_kinds:
                database.audit(
                    "analysis.skipped_accepted",
                    {
                        "analyzer": analyzer.name,
                        "kinds": sorted(analyzer.output_kinds & accepted_kinds),
                    },
                    job_id,
                    "worker",
                )
                continue
            suggestions.extend(analyzer.analyze(context))
        records = [suggestion.to_record() for suggestion in suggestions]
        database.add_suggestions(job_id, job["manifest_hash"], records)
        database.transition(job_id, "needs_review")
        return records
    except Exception as exc:
        database.transition(job_id, "failed", error=str(exc))
        raise
