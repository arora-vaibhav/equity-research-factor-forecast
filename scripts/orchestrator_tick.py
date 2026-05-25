"""CLI: run one tick of the rate-limited fetch orchestrator.

Usage::

    venv/Scripts/python.exe scripts/orchestrator_tick.py [--budget N] [--batch N]
                                                          [--db PATH]
                                                          [--config PATH]
                                                          [--json]

Defaults:
  --budget 60      seconds of wall-clock budget per tick
  --batch 10       dequeue batch size per inner loop
  --db   data/trade_identifier.db  database path
  --config config/orchestrator.yaml

Exit codes:
  0  - tick completed (including all-failures or fully-skipped); see stats
  1  - construction failure (bad config, missing DB) before any tick work

Output:
  Single line of stats by default, JSON on stdout with --json.

This is intentionally a bare CLI: no log file, no daemonisation. A
scheduler (Task Scheduler / cron) invokes it on whatever cadence the
operator wants. The orchestrator's audit trail lives in
``provider_call_log`` + ``fetch_queue``; the CLI is the dispatch
trigger, not the log of record.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Make ``src.*`` importable when the CLI is run from anywhere.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.common.database import DatabaseManager  # noqa: E402
from src.common.datasources.registry import DataSourceRegistry  # noqa: E402
from src.common.orchestrator.fetch_orchestrator import (  # noqa: E402
    RateLimitedFetchOrchestrator,
)


_DEFAULT_DB = _REPO_ROOT / "data" / "trade_identifier.db"
_DEFAULT_CONFIG = _REPO_ROOT / "config" / "orchestrator.yaml"


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="orchestrator_tick.py",
        description="Run one tick of the rate-limited fetch orchestrator.",
    )
    p.add_argument(
        "--budget", type=int, default=60,
        help="wall-clock budget (seconds) for this tick (default: 60)",
    )
    p.add_argument(
        "--batch", type=int, default=10,
        help="dequeue batch size (default: 10)",
    )
    p.add_argument(
        "--db", type=Path, default=_DEFAULT_DB,
        help=f"database path (default: {_DEFAULT_DB})",
    )
    p.add_argument(
        "--config", type=Path, default=_DEFAULT_CONFIG,
        help=f"orchestrator config path (default: {_DEFAULT_CONFIG})",
    )
    p.add_argument(
        "--json", action="store_true",
        help="emit stats as JSON on stdout (default: one-line summary)",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    try:
        db = DatabaseManager(db_path=str(args.db))
        registry = DataSourceRegistry()
        orch = RateLimitedFetchOrchestrator(
            db=db, registry=registry, config_path=args.config,
        )
    except Exception as e:  # noqa: BLE001 - CLI surface for any init failure
        print(f"orchestrator init failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    stats = orch.tick(budget_seconds=args.budget, batch_size=args.batch)

    if args.json:
        print(json.dumps(stats))
    else:
        print(
            f"attempted={stats['attempted']} "
            f"succeeded={stats['succeeded']} "
            f"failed={stats['failed']} "
            f"skipped_quota={stats['skipped_quota']} "
            f"orphans_reclaimed={stats['orphans_reclaimed']} "
            f"exited_at_deadline={stats['exited_at_deadline']}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
