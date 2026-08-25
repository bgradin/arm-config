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


def conditional_title_chain_context():
    sources = [source(10), source(20), source(30)]

    def play(number: int) -> dict:
        return {
            "operation": "play_playlist",
            "group_name": "branch",
            "dst": number,
            "dst_operand": {"type": "immediate", "value": number},
        }

    def compare_and_call(title: int) -> list[dict]:
        return [
            {
                "operation": "equal",
                "group_name": "compare",
                "dst": 4090,
                "src": 49,
                "dst_operand": {"type": "gpr", "number": 4090},
                "src_operand": {"type": "immediate", "value": 49},
            },
            {
                "operation": "call_title",
                "group_name": "branch",
                "dst": title,
                "dst_operand": {"type": "immediate", "value": title},
            },
            {
                "operation": "jump_title",
                "group_name": "branch",
                "dst": 0,
                "dst_operand": {"type": "immediate", "value": 0},
            },
        ]

    objects = [
        {
            "object_number": 100,
            "commands": [play(20), *compare_and_call(2)],
        },
        {
            "object_number": 101,
            "commands": [play(10), *compare_and_call(3)],
        },
        {
            "object_number": 102,
            "commands": [
                play(30),
                {
                    "operation": "jump_title",
                    "group_name": "branch",
                    "dst": 0,
                    "dst_operand": {"type": "immediate", "value": 0},
                },
            ],
        },
    ]
    titles = [
        {
            "title_number": 1,
            "object_type": {"name": "HDMV"},
            "object": {"id_ref": 100},
        },
        {
            "title_number": 2,
            "object_type": {"name": "HDMV"},
            "object": {"id_ref": 101},
        },
        {
            "title_number": 3,
            "object_type": {"name": "HDMV"},
            "object": {"id_ref": 102},
        },
    ]
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
    ]
    return SimpleNamespace(
        manifest={
            "disc": {"type": "bluray"},
            "navigation": {
                "index": {"indexes": {"titles": titles}},
                "movie_object": {
                    "movie_objects": {"objects": objects},
                },
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

    def test_missing_asset_mapping_does_not_reduce_confidence(self) -> None:
        result = HdmvNavigationStrategy().infer_order(context(mapped=False))

        self.assertEqual(result[0].confidence, 0.96)
        self.assertEqual(result[0].contradictions, [])

    def test_control_flow_prevents_play_sequence_claim(self) -> None:
        result = HdmvNavigationStrategy().infer_order(context(branched=True))

        # With no title-table references there is no suggestion at all; the
        # source-number fallback is emitted by its separate strategy.
        self.assertEqual(result, [])

    def test_conditional_title_chain_uses_title_numbers_for_order(self) -> None:
        result = HdmvNavigationStrategy().infer_order(
            conditional_title_chain_context()
        )

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].confidence, 0.97)
        self.assertEqual(
            result[0].value["source_keys"],
            ["mpls:00020", "mpls:00010", "mpls:00030"],
        )
        self.assertEqual(result[0].value["basis"], "hdmv_conditional_title_chain")
        self.assertEqual(result[0].value["titles"], [1, 2, 3])
        self.assertEqual(
            result[0].value["transitions"][0]["comparison"]["right"],
            {"type": "immediate", "value": 49},
        )


if __name__ == "__main__":
    unittest.main()
