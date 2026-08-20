from __future__ import annotations

from ..domain import Evidence, Suggestion
from .base import OrderContext, OrderStrategy, episode_cluster


class PlaylistHeuristicStrategy(OrderStrategy):
    name = "playlist_heuristics"

    def infer_order(self, context: OrderContext) -> list[Suggestion]:
        cluster = episode_cluster(context.sources)
        if len(cluster) < 2:
            return []
        ordered = sorted(cluster, key=lambda source: source["source_key"])
        return [
            Suggestion(
                kind="episode_order",
                value={
                    "source_ids": [source["id"] for source in ordered],
                    "source_keys": [source["source_key"] for source in ordered],
                    "basis": "source_identifier",
                },
                confidence=0.3,
                evidence=[
                    Evidence(
                        "stable_identifier_order",
                        "Source identifiers provide a deterministic fallback order only.",
                        0.3,
                    )
                ],
                contradictions=[
                    Evidence(
                        "no_navigation_intent",
                        "Playlist and title numbers do not universally encode playback order.",
                        -0.5,
                    )
                ],
                analyzer=self.name,
                analyzer_version=self.version,
            )
        ]
