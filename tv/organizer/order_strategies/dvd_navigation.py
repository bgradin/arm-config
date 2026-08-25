from __future__ import annotations

from ..domain import Evidence, Suggestion
from .base import (
    OrderContext,
    OrderStrategy,
    content_relationship_issues,
    episode_cluster,
)


class DvdNavigationStrategy(OrderStrategy):
    """Consume a libdvdnav/VM trace captured while the DVD was mounted."""

    name = "dvd_navigation"

    def infer_order(self, context: OrderContext) -> list[Suggestion]:
        if context.manifest.get("disc", {}).get("type") != "dvd":
            return []
        trace = (
            context.manifest.get("navigation", {})
            .get("runtime_traces", {})
            .get("dvd")
        )
        if not trace or trace.get("status") != "complete":
            return []
        source_keys = [str(item) for item in trace.get("source_keys", [])]
        cluster_keys = {item["source_key"] for item in episode_cluster(context.sources)}
        if (
            not source_keys
            or len(set(source_keys)) != len(source_keys)
            or set(source_keys) != cluster_keys
            or len(source_keys) != len(cluster_keys)
        ):
            return []
        by_key = {source["source_key"]: source for source in context.sources}
        try:
            ordered = [by_key[key] for key in source_keys]
        except KeyError:
            return []
        relationships = content_relationship_issues(ordered)
        branches = trace.get("unresolved_branches", [])
        play_all = trace.get("intent") == "play_all"
        high_confidence = not branches and not relationships and play_all
        return [
            Suggestion(
                kind="episode_order",
                value={
                    "source_ids": [item["id"] for item in ordered],
                    "source_keys": source_keys,
                    "basis": "dvd_navigation_trace",
                },
                confidence=0.96 if high_confidence else 0.55,
                evidence=[
                    Evidence(
                        "dvd_play_all_trace",
                        "A completed DVD navigation trace recorded this title/PGC sequence.",
                        0.96,
                        {"trace_id": trace.get("id")},
                    )
                ],
                contradictions=[
                    *(
                        [
                            Evidence(
                                "unresolved_dvd_vm_branch",
                                "DVD VM branches remain unresolved.",
                                -0.35,
                                {"branches": branches},
                            )
                        ]
                        if branches
                        else []
                    ),
                    *(
                        [
                            Evidence(
                                "unresolved_content_relationship",
                                "The trace includes unresolved duplicate or edition candidates.",
                                -0.35,
                                {"relationships": relationships},
                            )
                        ]
                        if relationships
                        else []
                    ),
                    *(
                        [
                            Evidence(
                                "trace_intent_not_play_all",
                                "The recorded interaction was not confirmed as Play All.",
                                -0.35,
                            )
                        ]
                        if not play_all
                        else []
                    ),
                ],
                analyzer=self.name,
                analyzer_version=self.version,
            )
        ]
