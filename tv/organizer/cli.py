from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .analyzers import analyze_job
from .collector import collect_disc
from .config import Config
from .db import Database
from .importer import attach_assets, import_manifest, register_manifest
from .planner import build_plan, commit_plan
from .service import serve
from .worker import Worker
from .workflow import approve_job


def _database(config: Config) -> Database:
    config.ensure_directories()
    database = Database(config.database_path)
    database.initialize()
    return database


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tv",
        description="Disc-aware TV ingest and organization service",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("init-db", help="Initialize the SQLite database")
    subparsers.add_parser("serve", help="Run the review web service")
    worker = subparsers.add_parser("worker", help="Run durable background tasks")
    worker.add_argument("--once", action="store_true")

    collect = subparsers.add_parser(
        "collect", help="Capture navigation metadata from a mounted disc"
    )
    collect.add_argument("--disc-root", type=Path, required=True)
    collect.add_argument("--arm-job-id")
    collect.add_argument("--makemkv-source")
    collect.add_argument("--makemkv-info-file", type=Path)
    collect.add_argument("--include-menu-assets", action="store_true")
    collect.add_argument("--volume-label")
    collect.add_argument(
        "--no-register",
        action="store_true",
        help="Capture without creating a persistent ripping job",
    )

    imported = subparsers.add_parser(
        "import", help="Import a completed capture and rip"
    )
    imported.add_argument("manifest", type=Path)
    imported.add_argument("--rip-root", type=Path, required=True)

    complete = subparsers.add_parser(
        "complete", help="Attach completed rip assets to a registered job"
    )
    complete.add_argument("job_id")
    complete.add_argument("--rip-root", type=Path, required=True)

    analyze = subparsers.add_parser("analyze", help="Analyze a job immediately")
    analyze.add_argument("job_id")
    plan = subparsers.add_parser("plan", help="Create a dry-run organization plan")
    plan.add_argument("job_id")
    approve = subparsers.add_parser("approve", help="Approve the latest dry-run plan")
    approve.add_argument("job_id")
    commit = subparsers.add_parser("commit", help="Commit an approved plan")
    commit.add_argument("job_id")
    commit.add_argument("--plan-id")
    subparsers.add_parser("list", help="List ingest jobs")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = Config.from_env()
    database = _database(config)
    try:
        if args.command == "init-db":
            print(config.database_path)
        elif args.command == "serve":
            serve(config, database)
        elif args.command == "worker":
            worker = Worker(config, database)
            if args.once:
                worker.run_once()
            else:
                worker.run_forever()
        elif args.command == "collect":
            manifest = collect_disc(
                config,
                args.disc_root,
                arm_job_id=args.arm_job_id,
                makemkv_source=args.makemkv_source,
                makemkv_info_file=args.makemkv_info_file,
                include_menu_assets=args.include_menu_assets,
                volume_label=args.volume_label,
            )
            value = {"manifest_path": str(manifest)}
            if not args.no_register:
                value["job_id"] = register_manifest(
                    database,
                    manifest,
                    state="ripping",
                )
            print(json.dumps(value, indent=2))
        elif args.command == "import":
            job_id = import_manifest(
                config,
                database,
                args.manifest,
                rip_root=args.rip_root,
                enqueue_analysis=False,
            )
            suggestions = analyze_job(config, database, job_id)
            print(json.dumps({"job_id": job_id, "suggestions": suggestions}, indent=2))
        elif args.command == "complete":
            attach_assets(
                config,
                database,
                args.job_id,
                args.rip_root,
                enqueue_analysis=False,
            )
            suggestions = analyze_job(config, database, args.job_id)
            print(json.dumps({"job_id": args.job_id, "suggestions": suggestions}, indent=2))
        elif args.command == "analyze":
            print(json.dumps(analyze_job(config, database, args.job_id), indent=2))
        elif args.command == "plan":
            print(json.dumps(build_plan(config, database, args.job_id), indent=2))
        elif args.command == "approve":
            approve_job(config, database, args.job_id)
            print(json.dumps({"job_id": args.job_id, "state": "approved"}))
        elif args.command == "commit":
            print(
                json.dumps(
                    commit_plan(config, database, args.job_id, args.plan_id),
                    indent=2,
                )
            )
        elif args.command == "list":
            print(json.dumps(database.list_jobs(), indent=2))
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1