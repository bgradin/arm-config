from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ..collector import collect_disc
from ..importer import ImportError, load_manifest

from .helpers import config_for


class CollectorTests(unittest.TestCase):
    def test_dvd_navigation_inputs_are_captured_immutably(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            disc = root / "MY_SHOW_S2_DISC1"
            video_ts = disc / "VIDEO_TS"
            video_ts.mkdir(parents=True)
            (video_ts / "VIDEO_TS.IFO").write_bytes(b"DVDVIDEO")
            (video_ts / "VIDEO_TS.BUP").write_bytes(b"DVDVIDEO")
            makemkv = root / "makemkv.txt"
            makemkv.write_text('CINFO:2,0,"MY_SHOW_S2_DISC1"\n', encoding="utf-8")
            config = config_for(root / "app")

            manifest_path = collect_disc(
                config,
                disc,
                makemkv_info_file=makemkv,
                volume_label="MY_SHOW_S2_DISC1",
            )
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

            self.assertEqual(manifest["disc"]["type"], "dvd")
            self.assertEqual(manifest["disc"]["volume_label"], "MY_SHOW_S2_DISC1")
            self.assertEqual(manifest["navigation"]["runtime_traces"]["dvd"]["status"], "not_captured")
            self.assertTrue(all(item["sha256"] for item in manifest["files"]))
            self.assertFalse(any("source_root" in item for item in manifest["files"]))
            load_manifest(manifest_path)

            captured_ifo = manifest_path.parent / "VIDEO_TS" / "VIDEO_TS.IFO"
            captured_ifo.write_bytes(b"changed")
            with self.assertRaises(ImportError):
                load_manifest(manifest_path)


if __name__ == "__main__":
    unittest.main()
