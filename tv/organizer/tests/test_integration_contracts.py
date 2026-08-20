from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from arm.bridge import _mounted_root
from arm.patch import (
    IDENTIFY_ANCHOR,
    IDENTIFY_REPLACEMENT,
    RIP_ANCHOR,
    RIP_REPLACEMENT,
    replace_once,
)
from ..importer import register_manifest
from ..service import Application
from ..web import static_asset

from .helpers import config_for, database_for, review_job


class ArmPatchContractTests(unittest.TestCase):
    def test_expected_arm_lifecycle_anchors_patch_once(self) -> None:
        source = f"before\n{IDENTIFY_ANCHOR}middle\n{RIP_ANCHOR}after\n"

        source = replace_once(source, IDENTIFY_ANCHOR, IDENTIFY_REPLACEMENT, "identify")
        source = replace_once(source, RIP_ANCHOR, RIP_REPLACEMENT, "rip")

        self.assertIn("capture_for_arm", source)
        self.assertIn("complete_for_arm", source)

    def test_upstream_drift_fails_closed(self) -> None:
        with self.assertRaises(RuntimeError):
            replace_once("changed upstream", IDENTIFY_ANCHOR, IDENTIFY_REPLACEMENT, "identify")

    def test_bridge_waits_for_disc_filesystem_after_mount(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            mountpoint = Path(temporary)
            job = SimpleNamespace(mountpoint=str(mountpoint), devpath="sr0")

            def mount_again(_command, **_options):
                (mountpoint / "BDMV").mkdir()
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            with patch("arm.bridge.subprocess.run", side_effect=mount_again):
                result = _mounted_root(job)

            self.assertEqual(mountpoint, result)


class ServiceSmokeTests(unittest.TestCase):
    def test_dashboard_and_review_page_render_without_network(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = config_for(Path(temporary))
            database = database_for(config)
            job_id = review_job(config, database, [("episode.mkv", b"episode")])
            app = Application(config, database)

            dashboard = app.dashboard().decode("utf-8")
            review = app.job_page(job_id, {}).decode("utf-8")

            self.assertIn("Ingest jobs", dashboard)
            self.assertIn('href="/static/app.css"', dashboard)
            self.assertIn('src="/static/app.js"', dashboard)
            self.assertNotIn("<style>", dashboard)
            self.assertIn("Example Show", review)
            self.assertIn("Background tasks", review)
            self.assertIn(f"/jobs/{job_id}/assets/asset-1", review)

            stylesheet, stylesheet_type = static_asset("app.css")
            script, script_type = static_asset("app.js")
            self.assertIn(b".card", stylesheet)
            self.assertEqual("text/css; charset=utf-8", stylesheet_type)
            self.assertIn(b"data-confirm", script)
            self.assertEqual("text/javascript; charset=utf-8", script_type)

    def test_templates_autoescape_review_data(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = config_for(Path(temporary))
            database = database_for(config)
            job_id = review_job(config, database, [("episode.mkv", b"episode")])
            database.resolve_job(job_id, {"show_name": "<script>alert(1)</script>"})

            review = Application(config, database).job_page(job_id, {}).decode("utf-8")

            self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", review)
            self.assertNotIn("<script>alert(1)</script>", review)


class IdempotencyTests(unittest.TestCase):
    def test_same_capture_reuses_job_but_intentional_recapture_does_not_collide(self) -> None:
        import json

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = config_for(root)
            database = database_for(config)
            manifest = {
                "schema_version": 1,
                "capture": {"arm_job_id": None},
                "disc": {"fingerprint": "a" * 64, "type": "dvd"},
                "navigation": {},
                "ripper": {
                    "titles": {
                        "0": {
                            "duration": "00:22:00",
                            "chapter_count": "4",
                            "segment_map": "1,2",
                            "streams": [],
                        }
                    }
                },
            }
            first_path = config.data_root / "capture-one.json"
            second_path = config.data_root / "capture-two.json"
            first_path.write_text(json.dumps(manifest), encoding="utf-8")
            second_path.write_text(json.dumps(manifest), encoding="utf-8")

            first = register_manifest(database, first_path)
            repeated = register_manifest(database, first_path)
            rerip = register_manifest(database, second_path)

            self.assertEqual(first, repeated)
            self.assertNotEqual(first, rerip)
            self.assertNotEqual(
                database.list_sources(first)[0]["id"],
                database.list_sources(rerip)[0]["id"],
            )


if __name__ == "__main__":
    unittest.main()
