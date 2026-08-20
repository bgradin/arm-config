from __future__ import annotations

import unittest
from types import SimpleNamespace

from ..order_strategies.hdmv_navigation import HdmvNavigationStrategy


def source(number: int) -> dict:
    return {
        "id": f"source-{number}",
        "source_key": f"mpls:{number:05d}",
        "duration_seconds": 1320 + number,
        "topology_hash": f"topology-{number}",
        "payload": {
            "playlist_id": number,
            "references": [],
            "topology": [{"clip_id": str(number), "in": 0, "out": 100}],
            "ripper_title": {
                "chapter_count": "2",
                "streams": [{"type": "Video"}, {"type": "Audio"}],
            },
        },
    }


def context(*, mapped: bool = True, branched: bool = False):
    sources = [source(10), source(20), source(30)]
    commands = []
    for number in (20, 10, 30):
        commands.append(
            {
                "operation": "play_playlist",
                "group_name": "branch",
                "dst": number,
                "dst_operand": {"type": "immediate", "value": number},
            }
        )
    if branched:
        commands.insert(1, {"operation": "goto", "group_name": "branch"})
    assets = [
        {
            "id": f"asset-{item['id']}",
            "source_title_id": item["id"],
            "duration_seconds": item["duration_seconds"],
            "metadata": {
                "source_mapping": {"method": "ripper_identity", "confidence": 0.98},
                "chapters": [{}, {}],
                "streams": [{"codec_type": "video"}, {"codec_type": "audio"}],
            },
        }
        for item in sources
    ] if mapped else []
    return SimpleNamespace(
        manifest={
            "disc": {"type": "bluray"},
            "navigation": {
                "movie_object": {
                    "movie_objects": {
                        "objects": [{"object_number": 7, "commands": commands}]
                    }
                }
            },
        },
        sources=sources,
        assets=assets,
    )


class HdmvOrderTests(unittest.TestCase):
    def test_unbranched_complete_sequence_can_be_high_confidence(self) -> None:
        result = HdmvNavigationStrategy().infer_order(context())

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].confidence, 0.96)
        self.assertEqual(
            result[0].value["source_ids"],
            ["source-20", "source-10", "source-30"],
        )

    def test_missing_asset_mapping_caps_confidence(self) -> None:
        result = HdmvNavigationStrategy().infer_order(context(mapped=False))

        self.assertLess(result[0].confidence, 0.7)
        self.assertEqual(result[0].contradictions[0].rule, "asset_mapping_not_one_to_one")

    def test_control_flow_prevents_play_sequence_claim(self) -> None:
        result = HdmvNavigationStrategy().infer_order(context(branched=True))

        # With no title-table references there is no suggestion at all; the
        # source-number fallback is emitted by its separate strategy.
        self.assertEqual(result, [])


if __name__ == "__main__":
    unittest.main()
