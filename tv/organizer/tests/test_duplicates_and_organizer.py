from __future__ import annotations

import tempfile
import unittest
import os
from pathlib import Path
from types import SimpleNamespace

from ..analyzers import DuplicateEditionAnalyzer
from ..planner import CommitError, build_plan, commit_plan
from ..workflow import approve_job, approval_errors

from .helpers import config_for, database_for, review_job


class DuplicateAnalysisTests(unittest.TestCase):
    def test_identical_topology_with_different_streams_is_a_stream_variant(self) -> None:
        topology = [{"clip_id": "00001", "in": 0, "out": 100}]
        sources = [
            {
                "id": "english",
                "source_key": "mpls:00001",
                "label": "English",
                "topology_hash": "same",
                "stream_fingerprint": "english-streams",
                "payload": {"topology": topology},
            },
            {
                "id": "commentary",
                "source_key": "mpls:00002",
                "label": "Commentary",
                "topology_hash": "same",
                "stream_fingerprint": "commentary-streams",
                "payload": {"topology": topology},
            },
        ]

        result = DuplicateEditionAnalyzer().analyze(
            SimpleNamespace(sources=sources, assets=[])
        )

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].kind, "stream_variant_group")
        self.assertEqual(result[0].value["classification"], "stream_variant")

    def test_branch_difference_is_an_edition_candidate(self) -> None:
        common = [
            {"clip_id": "00001", "in": 0, "out": 100},
            {"clip_id": "00002", "in": 0, "out": 100},
        ]
        sources = [
            {
                "id": "regular",
                "source_key": "mpls:00001",
                "topology_hash": "regular-hash",
                "duration_seconds": 1200,
                "payload": {"topology": common + [{"clip_id": "00003", "in": 0, "out": 50}]},
            },
            {
                "id": "directors",
                "source_key": "mpls:00002",
                "topology_hash": "directors-hash",
                "duration_seconds": 1260,
                "payload": {"topology": common + [{"clip_id": "00004", "in": 0, "out": 70}]},
            },
        ]
        context = SimpleNamespace(sources=sources, assets=[])

        result = DuplicateEditionAnalyzer().analyze(context)

        edition = next(item for item in result if item.kind == "edition_group")
        self.assertEqual(edition.value["classification"], "distinct_edition_candidate")
        self.assertGreaterEqual(edition.value["shared_topology_ratio"], 0.5)


class OrganizerTests(unittest.TestCase):
    def test_library_change_after_plan_blocks_commit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = config_for(Path(temporary), versions=False)
            database = database_for(config)
            job_id = review_job(config, database, [("episode.mkv", b"episode")])
            database.assign_asset(
                job_id,
                "asset-1",
                disposition="episode",
                season=1,
                episode_start=1,
            )
            plan = build_plan(config, database, job_id)
            approve_job(config, database, job_id)
            show_root = Path(plan["show_root"])
            show_root.mkdir(parents=True)
            (show_root / "unexpected.nfo").write_text("changed", encoding="utf-8")

            with self.assertRaises(CommitError):
                commit_plan(config, database, job_id, plan["id"])

            self.assertEqual(database.get_job(job_id)["state"], "approved")
            self.assertTrue(Path(plan["operations"][0]["source"]).exists())

    def test_interrupted_rename_is_compensated_then_committed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = config_for(Path(temporary), versions=False)
            database = database_for(config)
            job_id = review_job(config, database, [("episode.mkv", b"episode")])
            database.assign_asset(
                job_id,
                "asset-1",
                disposition="episode",
                season=1,
                episode_start=1,
            )
            plan = build_plan(config, database, job_id)
            approve_job(config, database, job_id)
            operation = plan["operations"][0]
            source = Path(operation["source"])
            target = Path(operation["target"])
            target.parent.mkdir(parents=True)
            database.transition(job_id, "organizing")
            os.replace(source, target)

            result = commit_plan(config, database, job_id, plan["id"])

            self.assertEqual(result["moved"], 1)
            self.assertTrue(target.exists())
            self.assertFalse(source.exists())
            events = [item["event_type"] for item in database.list_audit(job_id)]
            self.assertIn("plan.interrupted_commit_recovered", events)

    def test_nonpreferred_directors_cut_is_retained_when_versions_are_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = config_for(Path(temporary), versions=False)
            database = database_for(config)
            job_id = review_job(
                config,
                database,
                [("regular.mkv", b"regular"), ("directors.mkv", b"directors")],
            )
            database.assign_asset(
                job_id,
                "asset-1",
                disposition="episode",
                season=1,
                episode_start=1,
                edition_name="Regular",
                preferred=True,
            )
            database.assign_asset(
                job_id,
                "asset-2",
                disposition="episode",
                season=1,
                episode_start=1,
                edition_name="Director's Cut",
            )

            self.assertEqual(approval_errors(config, database, job_id), [])
            plan = build_plan(config, database, job_id)
            approve_job(config, database, job_id)

            moves = [item for item in plan["operations"] if item["action"] == "move"]
            retained = [item for item in plan["operations"] if item["action"] == "retain"]
            self.assertEqual(len(moves), 1)
            self.assertEqual(retained[0]["reason"], "non_preferred_edition")

            result = commit_plan(config, database, job_id, plan["id"])
            self.assertEqual(result["moved"], 1)
            self.assertTrue(Path(retained[0]["source"]).exists())
            self.assertTrue(Path(moves[0]["target"]).exists())
            self.assertEqual(database.get_job(job_id)["state"], "complete")

    def test_multipart_episode_publishes_every_part(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = config_for(Path(temporary), versions=False)
            database = database_for(config)
            job_id = review_job(
                config,
                database,
                [("part1.mkv", b"one"), ("part2.mkv", b"two")],
            )
            for number in (1, 2):
                database.assign_asset(
                    job_id,
                    f"asset-{number}",
                    disposition="episode",
                    season=1,
                    episode_start=2,
                    part=number,
                )

            self.assertEqual(approval_errors(config, database, job_id), [])
            plan = build_plan(config, database, job_id)
            approve_job(config, database, job_id)
            targets = [item["target"] for item in plan["operations"]]

            self.assertEqual(len(targets), 2)
            self.assertTrue(any("-part-1" in item for item in targets))
            self.assertTrue(any("-part-2" in item for item in targets))


if __name__ == "__main__":
    unittest.main()
