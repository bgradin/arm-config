from __future__ import annotations

import unittest

from .. import bdmv


class HdmvInstructionTests(unittest.TestCase):
    def test_play_playlist_is_semantically_decoded(self) -> None:
        word = (1 << 29) | (2 << 24) | (1 << 23)
        raw = word.to_bytes(4, "big") + (42).to_bytes(4, "big") + bytes(4)

        instruction = bdmv.parse_hdmv_instruction(raw)

        self.assertEqual(instruction.group_name, "branch")
        self.assertEqual(instruction.sub_group_name, "play")
        self.assertEqual(instruction.operation, "play_playlist")
        self.assertEqual(instruction.dst_operand, {"type": "immediate", "value": 42})

    def test_register_operand_is_not_reported_as_immediate(self) -> None:
        word = (2 << 29) | (1 << 27) | (2 << 8)
        raw = word.to_bytes(4, "big") + (3).to_bytes(4, "big") + (4).to_bytes(4, "big")

        instruction = bdmv.parse_hdmv_instruction(raw)

        self.assertEqual(instruction.operation, "equal")
        self.assertEqual(instruction.dst_operand["type"], "gpr")
        self.assertEqual(instruction.src_operand["type"], "gpr")


if __name__ == "__main__":
    unittest.main()

