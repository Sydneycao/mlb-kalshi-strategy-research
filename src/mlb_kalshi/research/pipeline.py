from __future__ import annotations

import csv
import json
import os
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pyarrow.parquet as pq  # type: ignore[import-untyped]
import structlog

from mlb_kalshi.config import Settings
from mlb_kalshi.research.execution import (
    execute_trade_plans,
    signal_records,
    summarize_executions,
)
from mlb_kalshi.research.models import ExecutionConfig
from mlb_kalshi.research.schemas import (
    EXECUTION_SCHEMA,
    SIGNAL_SCHEMA,
    SUMMARY_SCHEMA,
    TIMELINE_SCHEMA,
)
from mlb_kalshi.research.strategies import (
    BuyTheDip,
    LateGameMomentum,
    PregameToLive,
    Strategy,
    ThreatResolution,
    generate_trade_plans,
)
from mlb_kalshi.research.timeline import MINUTE, build_minute_timeline
from mlb_kalshi.storage import NormalizedStore, new_run_id, write_manifest

_STRATEGIES: dict[str, Callable[[], Strategy]] = {
    "pregame_to_live": PregameToLive,
    "buy_the_dip": BuyTheDip,
    "threat_resolution": ThreatResolution,
    "late_game_momentum": LateGameMomentum,
}


class BacktestPipeline:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._log = structlog.get_logger("backtest")

    def run(
        self,
        *,
        input_run: str | None,
        strategy_names: list[str] | None,
        pregame_minutes: int,
        contracts_per_trade: Decimal,
        max_volume_participation: Decimal,
        fee_rounding_quantum: Decimal,
    ) -> dict[str, Any]:
        input_manifest_path = resolve_input_manifest(
            self.settings.output_dir, input_run
        )
        input_manifest = json.loads(input_manifest_path.read_text(encoding="utf-8"))
        if input_manifest.get("run_type") != "smoke":
            raise ValueError("backtest input must be a completed smoke run")

        selected_strategies = _select_strategies(strategy_names)
        normalized_files = input_manifest.get("normalized_files", {})
        input_run_id = str(input_manifest["run_id"])
        matches = _read_records(
            _input_file(
                self.settings.output_dir,
                input_run_id,
                normalized_files,
                "matches",
                "game_matches.parquet",
            )
        )
        markets = _read_records(
            _input_file(
                self.settings.output_dir,
                input_run_id,
                normalized_files,
                "markets",
                "kalshi_markets.parquet",
            )
        )
        candles = _read_records(
            _input_file(
                self.settings.output_dir,
                input_run_id,
                normalized_files,
                "candlesticks",
                "kalshi_candlesticks_1m.parquet",
            )
        )
        trades = _read_records(
            _input_file(
                self.settings.output_dir,
                input_run_id,
                normalized_files,
                "trades",
                "kalshi_trades.parquet",
            )
        )
        raw_dir = _raw_dir(self.settings.output_dir, input_run_id, input_manifest)
        execution_config = ExecutionConfig(
            contracts_per_trade=contracts_per_trade,
            max_volume_participation=max_volume_participation,
            fee_rounding_quantum=fee_rounding_quantum,
        )
        execution_config.validate()

        timeline = build_minute_timeline(
            matches=matches,
            markets=markets,
            candles=candles,
            raw_dir=raw_dir,
            pregame_minutes=pregame_minutes,
        )
        plans = generate_trade_plans(timeline, selected_strategies)
        signals = signal_records(plans)
        executions = execute_trade_plans(
            plans,
            timeline,
            trades,
            execution_config,
        )
        summaries = summarize_executions(executions)
        validate_no_lookahead(signals, executions)

        counts = {
            "timeline_rows": len(timeline),
            "trade_plans": len(plans),
            "signal_rows": len(signals),
            "execution_rows": len(executions),
            "filled_execution_rows": sum(
                row["status"] == "filled" for row in executions
            ),
            "filled_trade_plans": sum(
                row["scenario"] == "base" and row["status"] == "filled"
                for row in executions
            ),
            "entry_unfilled_trade_plans": sum(
                row["scenario"] == "base" and row["status"] == "entry_unfilled"
                for row in executions
            ),
            "exit_unfilled_trade_plans": sum(
                row["scenario"] == "base" and row["status"] == "exit_unfilled"
                for row in executions
            ),
        }
        quote_quality = {
            "timeline_minutes": len(timeline),
            "candle_observed_minutes": sum(
                bool(row["quote_observed"]) for row in timeline
            ),
            "missing_yes_bid_open_minutes": sum(
                bool(row["missing_yes_bid_open"]) for row in timeline
            ),
            "missing_yes_ask_open_minutes": sum(
                bool(row["missing_yes_ask_open"]) for row in timeline
            ),
            "two_sided_open_spread_minutes": sum(
                row["yes_open_spread"] is not None for row in timeline
            ),
        }
        base_executions = [
            row for row in executions if row["scenario"] == "base"
        ]
        capacity_quality = {
            "trade_rows": len(trades),
            "insufficient_entry_capacity_minutes": sum(
                int(row["insufficient_entry_capacity_minutes"])
                for row in base_executions
            ),
            "insufficient_exit_capacity_minutes": sum(
                int(row["insufficient_exit_capacity_minutes"])
                for row in base_executions
            ),
            "entry_unfilled_trade_plans": counts["entry_unfilled_trade_plans"],
            "exit_unfilled_trade_plans": counts["exit_unfilled_trade_plans"],
        }
        run_id = new_run_id("backtest")
        output_dir = self.settings.output_dir / "research" / run_id
        output_dir.mkdir(parents=True, exist_ok=False)
        store = NormalizedStore(output_dir)
        files = {
            "timeline": store.write(
                "minute_game_market_timeline", timeline, TIMELINE_SCHEMA
            ),
            "signals": store.write("strategy_signals", signals, SIGNAL_SCHEMA),
            "executions": store.write(
                "strategy_executions", executions, EXECUTION_SCHEMA
            ),
            "summary": store.write(
                "strategy_summary", summaries, SUMMARY_SCHEMA
            ),
        }
        files["summary_csv"] = _write_summary_csv(
            output_dir / "strategy_summary.csv", summaries
        )
        files["report"] = _write_markdown_report(
            output_dir / "strategy_report.md",
            input_run_id=input_run_id,
            summaries=summaries,
            quote_quality=quote_quality,
            capacity_quality=capacity_quality,
            execution_config=execution_config,
        )
        manifest = {
            "run_id": run_id,
            "run_type": "backtest",
            "input_run_id": input_run_id,
            "configuration": {
                "pregame_minutes": pregame_minutes,
                "strategies": [strategy.name for strategy in selected_strategies],
                "execution_scenarios": ["base", "adverse_1cent"],
                "buy_price_rule": "next available YES ask open after signal minute closes",
                "sell_price_rule": "next available YES bid open after signal minute closes",
                "same_minute_extrema_executable": False,
                "contracts_per_trade": str(execution_config.contracts_per_trade),
                "capacity_rule": (
                    "all-or-none; at-or-better public trade volume in the "
                    "execution minute times max_volume_participation"
                ),
                "max_volume_participation": str(
                    execution_config.max_volume_participation
                ),
                "fee_type": "quadratic_with_maker_fees",
                "fee_side": "taker",
                "taker_fee_rate": str(execution_config.taker_fee_rate),
                "fee_multiplier": str(execution_config.fee_multiplier),
                "fee_rounding_quantum_dollars": str(
                    execution_config.fee_rounding_quantum
                ),
                "fees_included": True,
            },
            "counts": counts,
            "quote_quality": quote_quality,
            "capacity_quality": capacity_quality,
            "strategy_summary": summaries,
            "files": {key: str(path.resolve()) for key, path in files.items()},
        }
        manifest_path = output_dir / "manifest.json"
        write_manifest(manifest_path, manifest)
        self._log.info("backtest_complete", **counts)
        return {**manifest, "manifest": str(manifest_path.resolve())}


def resolve_input_manifest(output_dir: Path, input_run: str | None) -> Path:
    if input_run:
        candidate = Path(input_run)
        if candidate.is_dir():
            candidate = candidate / "manifest.json"
        if candidate.is_file():
            return candidate
        candidate = output_dir / "runs" / input_run / "manifest.json"
        if candidate.is_file():
            return candidate
        raise FileNotFoundError(f"smoke run manifest not found: {input_run}")

    candidates = sorted((output_dir / "runs").glob("smoke_*/manifest.json"))
    if not candidates:
        raise FileNotFoundError("no smoke run manifests found under data/runs")
    return candidates[-1]


def validate_no_lookahead(
    signals: list[dict[str, Any]], executions: list[dict[str, Any]]
) -> None:
    for signal in signals:
        if (
            signal["signal_available_at_utc"]
            != signal["observed_minute_start_utc"] + MINUTE
        ):
            raise AssertionError("signal must become available only after minute close")

    base_by_plan: dict[str, dict[str, Any]] = {}
    adverse_by_plan: dict[str, dict[str, Any]] = {}
    for execution in executions:
        if execution["price_rule"] != "next_minute_open_quote":
            raise AssertionError("execution used a forbidden price source")
        entry_time = execution.get("entry_execution_at_utc")
        if (
            entry_time is not None
            and entry_time < execution["entry_signal_after_utc"]
        ):
            raise AssertionError("purchase preceded signal availability")
        exit_time = execution.get("exit_execution_at_utc")
        effective_exit = execution.get("effective_exit_available_at_utc")
        if exit_time is not None and (
            effective_exit is None or exit_time < effective_exit
        ):
            raise AssertionError("sale preceded signal availability")
        target = (
            base_by_plan
            if execution["scenario"] == "base"
            else adverse_by_plan
        )
        target[str(execution["plan_id"])] = execution

    for plan_id, base in base_by_plan.items():
        adverse = adverse_by_plan.get(plan_id)
        if adverse is None:
            raise AssertionError("missing adverse-slippage scenario")
        base_pnl = base.get("pnl_dollars")
        adverse_pnl = adverse.get("pnl_dollars")
        if (
            isinstance(base_pnl, Decimal)
            and isinstance(adverse_pnl, Decimal)
            and adverse_pnl > base_pnl
        ):
            raise AssertionError("adverse slippage improved simulated PnL")


def _select_strategies(names: list[str] | None) -> list[Strategy]:
    selected = list(_STRATEGIES) if not names or names == ["all"] else names
    unknown = sorted(set(selected) - set(_STRATEGIES))
    if unknown:
        raise ValueError(
            f"unknown strategies: {', '.join(unknown)}; "
            f"choose from {', '.join(_STRATEGIES)}"
        )
    return [_STRATEGIES[name]() for name in selected]


def _read_records(path: Path) -> list[dict[str, Any]]:
    return cast(list[dict[str, Any]], pq.read_table(path).to_pylist())


def _input_file(
    output_dir: Path,
    input_run_id: str,
    manifest_files: dict[str, Any],
    key: str,
    filename: str,
) -> Path:
    manifest_path = Path(str(manifest_files.get(key, "")))
    if manifest_path.is_file():
        return manifest_path
    fallback = output_dir / "normalized" / input_run_id / filename
    if fallback.is_file():
        return fallback
    raise FileNotFoundError(f"normalized input not found: {filename}")


def _raw_dir(
    output_dir: Path, input_run_id: str, manifest: dict[str, Any]
) -> Path:
    manifest_path = Path(str(manifest.get("raw_dir", "")))
    if manifest_path.is_dir():
        return manifest_path
    fallback = output_dir / "raw" / input_run_id
    if fallback.is_dir():
        return fallback
    raise FileNotFoundError(f"raw input directory not found for {input_run_id}")


def _write_summary_csv(
    path: Path, summaries: list[dict[str, Any]]
) -> Path:
    fieldnames = list(summaries[0]) if summaries else []
    temp_path = path.with_name(f".{path.name}.tmp")
    with temp_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        if fieldnames:
            writer.writeheader()
            writer.writerows(summaries)
    os.replace(temp_path, path)
    return path


def _write_markdown_report(
    path: Path,
    *,
    input_run_id: str,
    summaries: list[dict[str, Any]],
    quote_quality: dict[str, int],
    capacity_quality: dict[str, int],
    execution_config: ExecutionConfig,
) -> Path:
    lines = [
        "# Strategy smoke-sample comparison",
        "",
        f"Input run: `{input_run_id}`",
        "",
        (
            f"These are {execution_config.contracts_per_trade}-contract diagnostics "
            "on a 10-game smoke sample. Net PnL includes quadratic Kalshi taker "
            "fees on entry and exit. Results are not evidence of profitability."
        ),
        "",
        "| Strategy | Scenario | Plans | Filled | Gross PnL | Fees | Net PnL | "
        "Win rate | Entry capacity waits | Exit capacity waits |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in summaries:
        lines.append(
            "| {strategy} | {scenario} | {plans} | {filled_trades} | "
            "{total_gross_pnl_dollars} | {total_fees_dollars} | "
            "{total_pnl_dollars} | {win_rate} | "
            "{insufficient_entry_capacity_minutes} | "
            "{insufficient_exit_capacity_minutes} |".format(
                **row
            )
        )
    lines.extend(
        [
            "",
            "## Quote quality",
            "",
            f"- Timeline minutes: {quote_quality['timeline_minutes']}",
            (
                "- Candle-observed minutes: "
                f"{quote_quality['candle_observed_minutes']}"
            ),
            (
                "- Missing YES bid-open minutes: "
                f"{quote_quality['missing_yes_bid_open_minutes']}"
            ),
            (
                "- Missing YES ask-open minutes: "
                f"{quote_quality['missing_yes_ask_open_minutes']}"
            ),
            "",
            "## Executable capacity",
            "",
            (
                "- Requested contracts per trade: "
                f"{execution_config.contracts_per_trade}"
            ),
            (
                "- Maximum share of compatible public trade volume: "
                f"{execution_config.max_volume_participation}"
            ),
            (
                "- Entry-unfilled plans: "
                f"{capacity_quality['entry_unfilled_trade_plans']}"
            ),
            (
                "- Exit-unfilled plans: "
                f"{capacity_quality['exit_unfilled_trade_plans']}"
            ),
            "",
            "Executions use only the next available minute-open YES ask for buys "
            "and YES bid for sells. An all-or-none order also requires sufficient "
            "same-minute public trade volume at that price or better after applying "
            "the participation cap. Same-minute extrema are never executable.",
            "",
        ]
    )
    temp_path = path.with_name(f".{path.name}.tmp")
    temp_path.write_text("\n".join(lines), encoding="utf-8")
    os.replace(temp_path, path)
    return path
