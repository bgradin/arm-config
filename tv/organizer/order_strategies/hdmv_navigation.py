from __future__ import annotations

from typing import Any

from ..domain import Evidence, Suggestion
from .base import (
    OrderContext,
    OrderStrategy,
    asset_mapping_issues,
    content_relationship_issues,
    episode_cluster,
)


PLAY_OPERATIONS = {
    "play_playlist",
    "play_playlist_at_play_item",
    "play_playlist_at_mark",
}
CONTROL_FLOW_OPERATIONS = {
    "goto",
    "break",
    "jump_object",
    "jump_title",
    "call_object",
    "call_title",
    "resume",
    "link_play_item",
    "link_mark",
}


def _playlist_id(source: dict[str, Any]) -> int | None:
    value = source.get("payload", {}).get("playlist_id")
    return int(value) if value is not None else None


def _play_sequence(commands: list[dict[str, Any]]) -> list[int]:
    return [
        int(command["dst"])
        for command in commands
        if command.get("operation") in PLAY_OPERATIONS
        and command.get("dst_operand", {}).get("type") == "immediate"
    ]


class HdmvNavigationStrategy(OrderStrategy):
    name = "hdmv_navigation"

    def _explicit_sequences(
        self,
        context: OrderContext,
        cluster: list[dict[str, Any]],
    ) -> list[tuple[int, list[dict[str, Any]]]]:
        by_playlist = {
            playlist_id: source
            for source in cluster
            if (playlist_id := _playlist_id(source)) is not None
        }
        wanted = set(by_playlist)
        result = []
        objects = (
            context.manifest.get("navigation", {})
            .get("movie_object", {})
            .get("movie_objects", {})
            .get("objects", [])
        )
        for movie_object in objects:
            commands = movie_object.get("commands", [])
            sequence = _play_sequence(commands)
            if len(sequence) != len(wanted) or set(sequence) != wanted:
                continue
            if len(set(sequence)) != len(sequence):
                continue
            unsafe = any(
                command.get("group_name") == "compare"
                or command.get("operation") in CONTROL_FLOW_OPERATIONS
                or str(command.get("operation", "")).startswith("unknown")
                for command in commands
            )
            if unsafe:
                continue
            result.append(
                (
                    int(movie_object.get("object_number", -1)),
                    [by_playlist[playlist_id] for playlist_id in sequence],
                )
            )
        return result

    def infer_order(self, context: OrderContext) -> list[Suggestion]:
        if context.manifest.get("disc", {}).get("type") != "bluray":
            return []
        cluster = episode_cluster(context.sources)
        if len(cluster) < 2:
            return []
        sequences = self._explicit_sequences(context, cluster)
        unique_orders = {
            tuple(source["id"] for source in ordered): (object_id, ordered)
            for object_id, ordered in sequences
        }
        if len(unique_orders) == 1:
            object_id, ordered = next(iter(unique_orders.values()))
            mapping_issues = asset_mapping_issues(ordered, context.assets)
            relationship_issues = content_relationship_issues(ordered)
            confidence = (
                0.96 if not mapping_issues and not relationship_issues else 0.58
            )
            contradictions = []
            if mapping_issues:
                contradictions.append(
                    Evidence(
                        "asset_mapping_not_one_to_one",
                        "Not every navigated source maps to exactly one duration-consistent rip asset.",
                        -0.38,
                        {"issues": mapping_issues},
                    )
                )
            if relationship_issues:
                contradictions.append(
                    Evidence(
                        "unresolved_content_relationship",
                        "Shared branch segments may represent duplicate titles or editorial editions.",
                        -0.38,
                        {"relationships": relationship_issues},
                    )
                )
            return [
                Suggestion(
                    kind="episode_order",
                    value={
                        "source_ids": [item["id"] for item in ordered],
                        "source_keys": [item["source_key"] for item in ordered],
                        "basis": "unbranched_hdmv_play_sequence",
                        "movie_object": object_id,
                    },
                    confidence=confidence,
                    evidence=[
                        Evidence(
                            "complete_hdmv_play_sequence",
                            "One unbranched HDMV object plays every episode candidate exactly once.",
                            0.96,
                            {"movie_object": object_id},
                        )
                    ],
                    contradictions=contradictions,
                    analyzer=self.name,
                    analyzer_version=self.version,
                )
            ]

        # Title-table ordering is useful to display, but it is not evidence of
        # Play All intent and therefore can never reach high confidence.
        title_rows = []
        ambiguous = []
        for source in cluster:
            refs = [
                ref
                for ref in source.get("payload", {}).get("references", [])
                if ref.get("title_number") is not None
            ]
            if len(refs) == 1:
                title_rows.append((int(refs[0]["title_number"]), source))
            else:
                ambiguous.append(source["source_key"])
        if len(title_rows) < 2:
            return []
        title_rows.sort(key=lambda item: item[0])
        return [
            Suggestion(
                kind="episode_order",
                value={
                    "source_ids": [item[1]["id"] for item in title_rows],
                    "source_keys": [item[1]["source_key"] for item in title_rows],
                    "basis": "hdmv_title_table_fallback",
                },
                confidence=0.48 if not ambiguous else 0.32,
                evidence=[
                    Evidence(
                        "index_title_order",
                        "The index provides a deterministic title-table order.",
                        0.48,
                    )
                ],
                contradictions=[
                    Evidence(
                        "title_order_is_not_play_all",
                        "No unambiguous Play All command sequence was found.",
                        -0.45,
                        {"ambiguous_sources": ambiguous, "sequence_count": len(unique_orders)},
                    )
                ],
                analyzer=self.name,
                analyzer_version=self.version,
            )
        ]