from __future__ import annotations

from typing import Any

from ..domain import Evidence, Suggestion
from .base import (
    OrderContext,
    OrderStrategy,
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
    version = "1.1.0"

    @staticmethod
    def _title_objects(
        context: OrderContext,
    ) -> dict[int, tuple[int, dict[str, Any]]]:
        """Return HDMV title number -> (movie object number, object).

        HDMV ``call_title`` operands refer to title numbers from
        ``index.bdmv``.  They do not refer to the movie-object numbers in
        ``MovieObject.bdmv``; keeping this mapping explicit avoids treating a
        title call as an object call.
        """

        navigation = context.manifest.get("navigation", {})
        objects = {
            int(item["object_number"]): item
            for item in navigation.get("movie_object", {})
            .get("movie_objects", {})
            .get("objects", [])
            if item.get("object_number") is not None
        }
        result: dict[int, tuple[int, dict[str, Any]]] = {}
        for title in navigation.get("index", {}).get("indexes", {}).get(
            "titles", []
        ):
            object_type = title.get("object_type", {})
            if object_type and object_type.get("name") not in {None, "HDMV"}:
                continue
            try:
                title_number = int(title["title_number"])
                object_number = int(title["object"]["id_ref"])
            except (KeyError, TypeError, ValueError):
                continue
            movie_object = objects.get(object_number)
            if movie_object is not None:
                result[title_number] = (object_number, movie_object)
        return result

    @staticmethod
    def _conditional_call_targets(
        commands: list[dict[str, Any]],
    ) -> list[tuple[int, dict[str, Any]]]:
        """Find title calls immediately guarded by an equality comparison.

        In HDMV, a compare followed by a branch operation is a common way to
        implement Play All.  We only accept an equality comparison against an
        immediate value, so arbitrary or unresolved navigation branches do
        not become episode-order evidence.
        """

        result = []
        for index, command in enumerate(commands):
            if command.get("operation") != "call_title":
                continue
            target = command.get("dst_operand", {})
            if target.get("type") != "immediate":
                continue
            try:
                target_title = int(target["value"])
            except (KeyError, TypeError, ValueError):
                continue
            if index == 0:
                continue
            comparison = commands[index - 1]
            if comparison.get("operation") != "equal":
                continue
            left = comparison.get("dst_operand", {})
            right = comparison.get("src_operand", {})
            if left.get("type") not in {"gpr", "psr"}:
                continue
            if right.get("type") != "immediate":
                continue
            result.append((target_title, comparison))
        return result

    def _conditional_title_chain_sequences(
        self,
        context: OrderContext,
        cluster: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Recover a Play All order expressed as a chain of title calls.

        Some discs put one episode in each title object and use a repeated
        ``compare ... immediate`` / ``call_title next`` pair to continue the
        sequence.  The playlist order is therefore encoded by the title-call
        graph, not by playlist identifiers or the order of the index table.
        """

        by_playlist = {
            playlist_id: source
            for source in cluster
            if (playlist_id := _playlist_id(source)) is not None
        }
        wanted = set(by_playlist)
        if len(wanted) != len(cluster):
            return []

        title_objects = self._title_objects(context)
        if not title_objects:
            return []

        title_playlists: dict[int, int] = {}
        title_conditions: dict[int, list[tuple[int, dict[str, Any]]]] = {}
        title_object_numbers: dict[int, int] = {}
        for title_number, (object_number, movie_object) in title_objects.items():
            commands = movie_object.get("commands", [])
            sequence = _play_sequence(commands)
            if len(sequence) != 1 or sequence[0] not in wanted:
                continue
            title_playlists[title_number] = sequence[0]
            title_conditions[title_number] = self._conditional_call_targets(
                commands
            )
            title_object_numbers[title_number] = object_number

        if len(title_playlists) < len(wanted):
            return []

        # Only calls into another episode title can define this episode
        # sequence. Calls to menus, first playback, or other extras are
        # intentionally ignored.
        edges: dict[int, tuple[int, dict[str, Any]]] = {}
        for title_number, calls in title_conditions.items():
            candidates = [
                (target, comparison)
                for target, comparison in calls
                if target in title_playlists and target != title_number
            ]
            if len(candidates) == 1:
                edges[title_number] = candidates[0]
            elif len(candidates) > 1:
                # A branch with multiple possible episode successors is not a
                # deterministic Play All order.
                continue

        incoming = {target for target, _ in edges.values()}
        starts = [title for title in title_playlists if title not in incoming]
        results = []
        for start in starts:
            titles = []
            current = start
            while current not in titles:
                titles.append(current)
                edge = edges.get(current)
                if edge is None:
                    break
                current = edge[0]

            if len(titles) != len(wanted):
                continue
            playlists = [title_playlists[title] for title in titles]
            if len(set(playlists)) != len(playlists) or set(playlists) != wanted:
                continue

            transitions = []
            for title in titles[:-1]:
                target, comparison = edges[title]
                transitions.append(
                    {
                        "from_title": title,
                        "to_title": target,
                        "comparison": {
                            "operation": comparison.get("operation"),
                            "left": comparison.get("dst_operand"),
                            "right": comparison.get("src_operand"),
                        },
                    }
                )
            results.append(
                {
                    "start_title": start,
                    "titles": titles,
                    "movie_objects": [title_object_numbers[title] for title in titles],
                    "transitions": transitions,
                    "ordered": [by_playlist[playlist] for playlist in playlists],
                }
            )
        return results

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

        chain_sequences = self._conditional_title_chain_sequences(context, cluster)
        unique_chain_orders = {
            tuple(source["id"] for source in item["ordered"]): item
            for item in chain_sequences
        }
        if len(unique_chain_orders) == 1:
            chain = next(iter(unique_chain_orders.values()))
            ordered = chain["ordered"]
            return [
                Suggestion(
                    kind="episode_order",
                    value={
                        "source_ids": [item["id"] for item in ordered],
                        "source_keys": [item["source_key"] for item in ordered],
                        "basis": "hdmv_conditional_title_chain",
                        "start_title": chain["start_title"],
                        "titles": chain["titles"],
                        "movie_objects": chain["movie_objects"],
                        "transitions": chain["transitions"],
                    },
                    confidence=0.97,
                    evidence=[
                        Evidence(
                            "conditional_hdmv_title_chain",
                            "HDMV title objects form a complete conditional Play All chain over every episode candidate.",
                            0.97,
                            {
                                "start_title": chain["start_title"],
                                "titles": chain["titles"],
                                "transitions": chain["transitions"],
                            },
                        )
                    ],
                    contradictions=[],
                    analyzer=self.name,
                    analyzer_version=self.version,
                )
            ]

        sequences = self._explicit_sequences(context, cluster)
        unique_orders = {
            tuple(source["id"] for source in ordered): (object_id, ordered)
            for object_id, ordered in sequences
        }
        if len(unique_orders) == 1:
            object_id, ordered = next(iter(unique_orders.values()))
            relationship_issues = content_relationship_issues(ordered)
            confidence = 0.96 if not relationship_issues else 0.58
            contradictions = []
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
