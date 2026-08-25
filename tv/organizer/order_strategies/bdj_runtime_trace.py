from __future__ import annotations

from ..domain import Evidence, Suggestion
from .base import (
    OrderContext,
    OrderStrategy,
    content_relationship_issues,
    episode_cluster,
)


class BdjRuntimeTraceStrategy(OrderStrategy):
    """Consume a sandbox-produced BD-J playlist-event trace when available.

    Trace generation is intentionally outside the web process.  A sandbox
    runner can supply ``navigation.runtime_traces.bdj`` before the collector
    seals the immutable manifest without changing this strategy contract.
    """

    name = "bdj_runtime_trace"

    def infer_order(self, context: OrderContext) -> list[Suggestion]:
        trace = (
            context.manifest.get("navigation", {})
            .get("runtime_traces", {})
            .get("bdj")
        )
        if not trace:
            return []
        if trace.get("status") != "complete" or not trace.get("playlists"):
            return []
        cluster = episode_cluster(context.sources)
        wanted = {
            int(source.get("payload", {}).get("playlist_id"))
            for source in cluster
            if source.get("payload", {}).get("playlist_id") is not None
        }
        by_playlist = {
            int(source.get("payload", {}).get("playlist_id")): source
            for source in context.sources
            if source.get("payload", {}).get("playlist_id") is not None
        }
        playlist_ids = [int(item) for item in trace["playlists"]]
        if (
            len(set(playlist_ids)) != len(playlist_ids)
            or set(playlist_ids) != wanted
            or len(playlist_ids) != len(wanted)
        ):
            return []
        try:
            ordered = [by_playlist[item] for item in playlist_ids]
        except KeyError:
            return []
        relationships = content_relationship_issues(ordered)
        branches = trace.get("unresolved_branches", [])
        play_all = trace.get("intent") == "play_all"
        eligible = not relationships and not branches and play_all
        return [
            Suggestion(
                kind="episode_order",
                value={
                    "source_ids": [item["id"] for item in ordered],
                    "source_keys": [item["source_key"] for item in ordered],
                    "basis": "bdj_runtime_trace",
                },
                confidence=0.97 if eligible else 0.58,
                evidence=[
                    Evidence(
                        "sandboxed_playlist_event_trace",
                        "A completed sandbox trace recorded this playlist sequence.",
                        0.97,
                        {"trace_id": trace.get("id")},
                    )
                ],
                contradictions=(
                    [
                        Evidence(
                            "unresolved_content_relationship",
                            "The trace includes unresolved duplicate or edition candidates.",
                            -0.39,
                            {"relationships": relationships},
                        )
                    ]
                    if relationships
                    else []
                ) + (
                    [
                        Evidence(
                            "trace_intent_or_branches_unresolved",
                            "The trace is not a confirmed, branch-complete Play All interaction.",
                            -0.39,
                            {"intent": trace.get("intent"), "branches": branches},
                        )
                    ]
                    if branches or not play_all
                    else []
                ),
                analyzer=self.name,
                analyzer_version=self.version,
            )
        ]
