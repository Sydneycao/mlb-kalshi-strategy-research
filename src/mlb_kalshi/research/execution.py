from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Literal

from mlb_kalshi.research.models import QuoteFill, TradePlan

CENT = Decimal("0.0100")
PRICE_QUANTUM = Decimal("0.0001")
RETURN_QUANTUM = Decimal("0.000001")
MINUTE = timedelta(minutes=1)


def signal_records(plans: list[TradePlan]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for plan in plans:
        parameters_json = json.dumps(plan.parameters, sort_keys=True)
        records.extend(
            [
                {
                    "plan_id": plan.plan_id,
                    "strategy": plan.strategy,
                    "event_ticker": plan.event_ticker,
                    "ticker": plan.ticker,
                    "action": "buy",
                    "observed_minute_start_utc": (
                        plan.entry_signal_minute_start_utc
                    ),
                    "signal_available_at_utc": plan.entry_signal_after_utc,
                    "reason": plan.entry_reason,
                    "parameters_json": parameters_json,
                },
                {
                    "plan_id": plan.plan_id,
                    "strategy": plan.strategy,
                    "event_ticker": plan.event_ticker,
                    "ticker": plan.ticker,
                    "action": "sell",
                    "observed_minute_start_utc": plan.exit_signal_minute_start_utc,
                    "signal_available_at_utc": plan.exit_signal_after_utc,
                    "reason": plan.exit_reason,
                    "parameters_json": parameters_json,
                },
            ]
        )
    return records


def execute_trade_plans(
    plans: list[TradePlan],
    timeline: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Execute only on subsequent minute opens, never current-minute extrema."""

    rows_by_ticker: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in timeline:
        rows_by_ticker[str(row["ticker"])].append(row)
    for rows in rows_by_ticker.values():
        rows.sort(key=lambda row: row["minute_start_utc"])

    executions: list[dict[str, Any]] = []
    for plan in plans:
        rows = rows_by_ticker[plan.ticker]
        entry, entry_missing = _find_next_quote(
            rows,
            action="buy",
            available_at=plan.entry_signal_after_utc,
        )
        if entry is None:
            executions.extend(
                _scenario_records(
                    plan=plan,
                    status="entry_unfilled",
                    entry=None,
                    exit_fill=None,
                    entry_missing=entry_missing,
                    exit_missing=0,
                    effective_exit_available_at=None,
                )
            )
            continue

        effective_exit_available_at = max(
            plan.exit_signal_after_utc,
            entry.execution_at_utc + MINUTE,
        )
        exit_fill, exit_missing = _find_next_quote(
            rows,
            action="sell",
            available_at=effective_exit_available_at,
        )
        status = "filled" if exit_fill is not None else "exit_unfilled"
        executions.extend(
            _scenario_records(
                plan=plan,
                status=status,
                entry=entry,
                exit_fill=exit_fill,
                entry_missing=entry_missing,
                exit_missing=exit_missing,
                effective_exit_available_at=effective_exit_available_at,
            )
        )
    return executions


def summarize_executions(
    executions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    grouped: defaultdict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in executions:
        grouped[(str(row["strategy"]), str(row["scenario"]))].append(row)

    summaries: list[dict[str, Any]] = []
    for (strategy, scenario), rows in sorted(grouped.items()):
        filled = [row for row in rows if row["status"] == "filled"]
        pnl_values = [
            row["pnl_dollars"] for row in filled if row["pnl_dollars"] is not None
        ]
        return_values = [
            row["return_on_cost"] for row in filled if row["return_on_cost"] is not None
        ]
        entry_spreads = [
            row["entry_spread"] for row in filled if row["entry_spread"] is not None
        ]
        exit_spreads = [
            row["exit_spread"] for row in filled if row["exit_spread"] is not None
        ]
        summaries.append(
            {
                "strategy": strategy,
                "scenario": scenario,
                "plans": len(rows),
                "filled_trades": len(filled),
                "entry_unfilled": sum(
                    row["status"] == "entry_unfilled" for row in rows
                ),
                "exit_unfilled": sum(
                    row["status"] == "exit_unfilled" for row in rows
                ),
                "winning_trades": sum(value > 0 for value in pnl_values),
                "win_rate": _ratio(
                    sum(value > 0 for value in pnl_values), len(pnl_values)
                ),
                "total_pnl_dollars": sum(pnl_values, start=Decimal("0.0000")),
                "mean_pnl_dollars": _mean(pnl_values, PRICE_QUANTUM),
                "mean_return_on_cost": _mean(return_values, RETURN_QUANTUM),
                "mean_entry_delay_seconds": _mean_int(
                    [int(row["entry_delay_seconds"]) for row in filled]
                ),
                "mean_exit_delay_seconds": _mean_int(
                    [int(row["exit_delay_seconds"]) for row in filled]
                ),
                "missing_entry_quote_minutes": sum(
                    int(row["missing_entry_quote_minutes"]) for row in rows
                ),
                "missing_exit_quote_minutes": sum(
                    int(row["missing_exit_quote_minutes"]) for row in rows
                ),
                "mean_entry_spread": _mean(entry_spreads, PRICE_QUANTUM),
                "mean_exit_spread": _mean(exit_spreads, PRICE_QUANTUM),
            }
        )
    return summaries


def _find_next_quote(
    rows: list[dict[str, Any]],
    *,
    action: Literal["buy", "sell"],
    available_at: datetime,
) -> tuple[QuoteFill | None, int]:
    quote_field = "yes_ask_open" if action == "buy" else "yes_bid_open"
    missing = 0
    for row in rows:
        execution_at = row["minute_start_utc"]
        if execution_at < available_at:
            continue
        quote = row.get(quote_field)
        if not isinstance(quote, Decimal):
            missing += 1
            continue
        bid = row.get("yes_bid_open")
        ask = row.get("yes_ask_open")
        typed_bid = bid if isinstance(bid, Decimal) else None
        typed_ask = ask if isinstance(ask, Decimal) else None
        spread = (
            (typed_ask - typed_bid).quantize(PRICE_QUANTUM)
            if typed_bid is not None and typed_ask is not None
            else None
        )
        return (
            QuoteFill(
                execution_at_utc=execution_at,
                price=quote,
                bid=typed_bid,
                ask=typed_ask,
                spread=spread,
                delay_seconds=int((execution_at - available_at).total_seconds()),
                missing_quote_minutes=missing,
            ),
            missing,
        )
    return None, missing


def _scenario_records(
    *,
    plan: TradePlan,
    status: str,
    entry: QuoteFill | None,
    exit_fill: QuoteFill | None,
    entry_missing: int,
    exit_missing: int,
    effective_exit_available_at: datetime | None,
) -> list[dict[str, Any]]:
    return [
        _execution_record(
            plan=plan,
            scenario=scenario,
            status=status,
            entry=entry,
            exit_fill=exit_fill,
            entry_missing=entry_missing,
            exit_missing=exit_missing,
            effective_exit_available_at=effective_exit_available_at,
        )
        for scenario in ("base", "adverse_1cent")
    ]


def _execution_record(
    *,
    plan: TradePlan,
    scenario: str,
    status: str,
    entry: QuoteFill | None,
    exit_fill: QuoteFill | None,
    entry_missing: int,
    exit_missing: int,
    effective_exit_available_at: datetime | None,
) -> dict[str, Any]:
    entry_price = (
        _scenario_price(entry.price, action="buy", scenario=scenario)
        if entry is not None
        else None
    )
    exit_price = (
        _scenario_price(exit_fill.price, action="sell", scenario=scenario)
        if exit_fill is not None
        else None
    )
    pnl = (
        (exit_price - entry_price).quantize(PRICE_QUANTUM)
        if entry_price is not None and exit_price is not None
        else None
    )
    return_on_cost = (
        (pnl / entry_price).quantize(RETURN_QUANTUM, rounding=ROUND_HALF_UP)
        if (
            pnl is not None
            and isinstance(entry_price, Decimal)
            and entry_price != Decimal(0)
        )
        else None
    )
    return {
        "plan_id": plan.plan_id,
        "strategy": plan.strategy,
        "scenario": scenario,
        "event_ticker": plan.event_ticker,
        "ticker": plan.ticker,
        "status": status,
        "entry_signal_after_utc": plan.entry_signal_after_utc,
        "entry_execution_at_utc": entry.execution_at_utc if entry else None,
        "entry_delay_seconds": entry.delay_seconds if entry else None,
        "missing_entry_quote_minutes": entry_missing,
        "entry_raw_ask": entry.price if entry else None,
        "entry_bid": entry.bid if entry else None,
        "entry_spread": entry.spread if entry else None,
        "entry_price": entry_price,
        "exit_signal_after_utc": plan.exit_signal_after_utc,
        "effective_exit_available_at_utc": effective_exit_available_at,
        "exit_execution_at_utc": exit_fill.execution_at_utc if exit_fill else None,
        "exit_delay_seconds": exit_fill.delay_seconds if exit_fill else None,
        "missing_exit_quote_minutes": exit_missing,
        "exit_raw_bid": exit_fill.price if exit_fill else None,
        "exit_ask": exit_fill.ask if exit_fill else None,
        "exit_spread": exit_fill.spread if exit_fill else None,
        "exit_price": exit_price,
        "holding_seconds": (
            int(
                (exit_fill.execution_at_utc - entry.execution_at_utc).total_seconds()
            )
            if entry is not None and exit_fill is not None
            else None
        ),
        "pnl_dollars": pnl,
        "return_on_cost": return_on_cost,
        "price_rule": "next_minute_open_quote",
    }


def _scenario_price(
    price: Decimal,
    *,
    action: Literal["buy", "sell"],
    scenario: str,
) -> Decimal:
    if scenario == "base":
        return price.quantize(PRICE_QUANTUM)
    if scenario != "adverse_1cent":
        raise ValueError(f"unknown execution scenario: {scenario}")
    slipped = price + CENT if action == "buy" else price - CENT
    return min(Decimal(1), max(Decimal(0), slipped)).quantize(PRICE_QUANTUM)


def _mean(values: list[Decimal], quantum: Decimal) -> Decimal | None:
    if not values:
        return None
    return (sum(values, start=Decimal(0)) / Decimal(len(values))).quantize(
        quantum, rounding=ROUND_HALF_UP
    )


def _mean_int(values: list[int]) -> Decimal | None:
    if not values:
        return None
    return (Decimal(sum(values)) / Decimal(len(values))).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )


def _ratio(numerator: int, denominator: int) -> Decimal | None:
    if denominator == 0:
        return None
    return (Decimal(numerator) / Decimal(denominator)).quantize(
        RETURN_QUANTUM, rounding=ROUND_HALF_UP
    )
