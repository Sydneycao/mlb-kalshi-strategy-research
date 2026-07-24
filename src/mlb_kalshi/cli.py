from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from mlb_kalshi.config import Settings
from mlb_kalshi.logging import configure_logging
from mlb_kalshi.pipeline import ResearchPipeline


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mlb-kalshi",
        description="Probe and smoke-test historical MLB/Kalshi market data.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    probe = subparsers.add_parser(
        "probe", help="Check availability of all required public API families."
    )
    probe.add_argument("--output-dir", type=Path)

    smoke = subparsers.add_parser(
        "smoke", help="Run the bounded historical ingestion and matching smoke test."
    )
    smoke.add_argument(
        "--max-games",
        type=int,
        default=None,
        help="Number of Kalshi game events to ingest (default: env or 10).",
    )
    smoke.add_argument("--output-dir", type=Path)
    return parser


def run(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        settings = Settings.from_env().with_overrides(
            max_games=getattr(args, "max_games", None),
            output_dir=args.output_dir,
        )
    except ValueError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    configure_logging(settings.log_level)
    pipeline = ResearchPipeline(settings)
    if args.command == "probe":
        summary = pipeline.probe()
        exit_code = 1 if summary["failed"] else 0
    else:
        summary = pipeline.smoke()
        exit_code = 1 if summary["counts"]["kalshi_games_selected"] == 0 else 0
    print(json.dumps(summary, indent=2, sort_keys=True, default=str))
    return exit_code


def main() -> None:
    raise SystemExit(run())
