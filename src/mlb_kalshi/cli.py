from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from datetime import date
from decimal import Decimal
from pathlib import Path

from mlb_kalshi.backfill import HistoricalBackfillPipeline
from mlb_kalshi.config import Settings
from mlb_kalshi.logging import configure_logging
from mlb_kalshi.pipeline import ResearchPipeline
from mlb_kalshi.research.models import ExecutionConfig
from mlb_kalshi.research.pipeline import BacktestPipeline


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mlb-kalshi",
        description="Ingest and research historical MLB/Kalshi market data.",
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

    backfill = subparsers.add_parser(
        "backfill",
        help="Run or resume a game-checkpointed historical batch ingestion.",
    )
    backfill.add_argument(
        "--job-id",
        required=True,
        help="Stable task name. Reusing it resumes the existing checkpoint.",
    )
    backfill.add_argument(
        "--start-date",
        type=_iso_date,
        help="Inclusive MLB date for a new task (YYYY-MM-DD).",
    )
    backfill.add_argument(
        "--end-date",
        type=_iso_date,
        help="Inclusive MLB date for a new task (YYYY-MM-DD).",
    )
    backfill.add_argument(
        "--max-games",
        type=int,
        default=None,
        help="Maximum events in the catalog (default: 500 for a new task).",
    )
    backfill.add_argument(
        "--batch-size",
        type=int,
        default=None,
        help="Completed/failed game attempts between consolidated checkpoints (default: 25).",
    )
    backfill.add_argument(
        "--max-games-this-run",
        type=int,
        default=None,
        help="Optional attempt cap for this invocation; rerun the same job-id to continue.",
    )
    backfill.add_argument("--output-dir", type=Path)

    backtest = subparsers.add_parser(
        "backtest",
        help="Build the minute timeline and run bias-safe strategy simulations.",
    )
    backtest.add_argument(
        "--input-run",
        default=None,
        help=(
            "Smoke/completed-backfill run ID or manifest path "
            "(default: latest completed local ingestion)."
        ),
    )
    backtest.add_argument(
        "--strategies",
        default="all",
        help=(
            "Comma-separated strategy names or 'all': pregame_to_live, "
            "buy_the_dip, threat_resolution, late_game_momentum."
        ),
    )
    backtest.add_argument(
        "--pregame-minutes",
        type=int,
        default=180,
        help="Minutes before scheduled start included in each market timeline.",
    )
    backtest.add_argument(
        "--contracts-per-trade",
        type=Decimal,
        default=Decimal("1.00"),
        help="All-or-none contract quantity requested for every trade (default: 1).",
    )
    backtest.add_argument(
        "--max-volume-participation",
        type=Decimal,
        default=Decimal("0.10"),
        help=(
            "Maximum fraction of same-minute, at-or-better public trade volume "
            "treated as executable capacity (default: 0.10)."
        ),
    )
    backtest.add_argument(
        "--fee-rounding-quantum",
        type=Decimal,
        choices=[Decimal("0.01"), Decimal("0.0001")],
        default=Decimal("0.01"),
        help=(
            "Fee/balance rounding precision: 0.01 for conservative non-direct "
            "retail modeling or 0.0001 for direct members (default: 0.01)."
        ),
    )
    backtest.add_argument("--output-dir", type=Path)
    return parser


def run(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        settings = Settings.from_env().with_overrides(
            max_games=args.max_games if args.command == "smoke" else None,
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
    elif args.command == "smoke":
        summary = pipeline.smoke()
        exit_code = 1 if summary["counts"]["kalshi_games_selected"] == 0 else 0
    elif args.command == "backfill":
        try:
            summary = HistoricalBackfillPipeline(settings).run(
                job_id=args.job_id,
                start_date=args.start_date,
                end_date=args.end_date,
                max_games=args.max_games,
                batch_size=args.batch_size,
                max_games_this_run=args.max_games_this_run,
            )
        except ValueError as exc:
            print(f"configuration error: {exc}", file=sys.stderr)
            return 2
        exit_code = 1 if summary["status"] in {"empty", "needs_retry"} else 0
    else:
        if args.pregame_minutes < 0:
            print("configuration error: pregame-minutes cannot be negative", file=sys.stderr)
            return 2
        execution_config = ExecutionConfig(
            contracts_per_trade=args.contracts_per_trade,
            max_volume_participation=args.max_volume_participation,
            fee_rounding_quantum=args.fee_rounding_quantum,
        )
        try:
            execution_config.validate()
        except ValueError as exc:
            print(f"configuration error: {exc}", file=sys.stderr)
            return 2
        strategy_names = [
            name.strip() for name in args.strategies.split(",") if name.strip()
        ]
        summary = BacktestPipeline(settings).run(
            input_run=args.input_run,
            strategy_names=strategy_names,
            pregame_minutes=args.pregame_minutes,
            contracts_per_trade=execution_config.contracts_per_trade,
            max_volume_participation=execution_config.max_volume_participation,
            fee_rounding_quantum=execution_config.fee_rounding_quantum,
        )
        exit_code = 0
    print(json.dumps(summary, indent=2, sort_keys=True, default=str))
    return exit_code


def main() -> None:
    raise SystemExit(run())


def _iso_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected YYYY-MM-DD") from exc
