from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timedelta
from decimal import ROUND_CEILING, ROUND_DOWN, ROUND_HALF_UP, Decimal
from typing import Any, Literal

from mlb_kalshi.research.models import (
    CONTRACT_QUANTUM,
    ExecutionConfig,
    QuoteFill,
    TradePlan,
)

CENT = Decimal("0.0100")
PRICE_QUANTUM = Decimal("0.0001")
MONEY_QUANTUM = Decimal("0.0001")
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
    trades: list[dict[str, Any]],
    config: ExecutionConfig,
) -> list[dict[str, Any]]:
    """Execute all-or-none orders on later minute opens with trade-tape capacity."""

    config.validate()

    rows_by_ticker: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in timeline:
        rows_by_ticker[str(row["ticker"])].append(row)
    for rows in rows_by_ticker.values():
        rows.sort(key=lambda row: row["minute_start_utc"])
    trades_by_ticker_minute = _index_trades(trades)

    executions: list[dict[str, Any]] = []
    for plan in plans:
        rows = rows_by_ticker[plan.ticker]
        entry, entry_missing, entry_insufficient = _find_next_quote(
            rows,
            ticker=plan.ticker,
            action="buy",
            available_at=plan.entry_signal_after_utc,
            requested_contracts=config.contracts_per_trade,
            max_volume_participation=config.max_volume_participation,
            trades_by_ticker_minute=trades_by_ticker_minute,
        )
        if entry is None:
            executions.extend(
                _scenario_records(
                    plan=plan,
                    config=config,
                    status="entry_unfilled",
                    entry=None,
                    exit_fill=None,
                    entry_missing=entry_missing,
                    exit_missing=0,
                    entry_insufficient=entry_insufficient,
                    exit_insufficient=0,
                    effective_exit_available_at=None,
                )
            )
            continue

        effective_exit_available_at = max(
            plan.exit_signal_after_utc,
            entry.execution_at_utc + MINUTE,
        )
        exit_fill, exit_missing, exit_insufficient = _find_next_quote(
            rows,
            ticker=plan.ticker,
            action="sell",
            available_at=effective_exit_available_at,
            requested_contracts=config.contracts_per_trade,
            max_volume_participation=config.max_volume_participation,
            trades_by_ticker_minute=trades_by_ticker_minute,
        )
        status = "filled" if exit_fill is not None else "exit_unfilled"
        executions.extend(
            _scenario_records(
                plan=plan,
                config=config,
                status=status,
                entry=entry,
                exit_fill=exit_fill,
                entry_missing=entry_missing,
                exit_missing=exit_missing,
                entry_insufficient=entry_insufficient,
                exit_insufficient=exit_insufficient,
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
        gross_pnl_values = [
            row["gross_pnl_dollars"]
            for row in filled
            if row["gross_pnl_dollars"] is not None
        ]
        fee_values = [
            row["total_fees_dollars"]
            for row in filled
            if row["total_fees_dollars"] is not None
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
                "total_filled_contracts": sum(
                    (
                        row["exit_filled_contracts"]
                        for row in filled
                        if row["exit_filled_contracts"] is not None
                    ),
                    start=Decimal("0.00"),
                ),
                "total_gross_pnl_dollars": sum(
                    gross_pnl_values, start=Decimal("0.0000")
                ),
                "total_fees_dollars": sum(
                    fee_values, start=Decimal("0.0000")
                ),
                "total_pnl_dollars": sum(pnl_values, start=Decimal("0.0000")),
                "mean_pnl_dollars": _mean(pnl_values, MONEY_QUANTUM),
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
                "insufficient_entry_capacity_minutes": sum(
                    int(row["insufficient_entry_capacity_minutes"])
                    for row in rows
                ),
                "insufficient_exit_capacity_minutes": sum(
                    int(row["insufficient_exit_capacity_minutes"])
                    for row in rows
                ),
                "mean_entry_spread": _mean(entry_spreads, PRICE_QUANTUM),
                "mean_exit_spread": _mean(exit_spreads, PRICE_QUANTUM),
            }
        )
    return summaries


def _find_next_quote(
    rows: list[dict[str, Any]],
    *,
    ticker: str,
    action: Literal["buy", "sell"],
    available_at: datetime,
    requested_contracts: Decimal,
    max_volume_participation: Decimal,
    trades_by_ticker_minute: dict[
        tuple[str, datetime], list[tuple[Decimal, Decimal]]
    ],
) -> tuple[QuoteFill | None, int, int]:
    quote_field = "yes_ask_open" if action == "buy" else "yes_bid_open"
    missing = 0
    insufficient = 0
    for row in rows:
        execution_at = row["minute_start_utc"]
        if execution_at < available_at:
            continue
        quote = row.get(quote_field)
        if not isinstance(quote, Decimal):
            missing += 1
            continue
        compatible_trade_volume = sum(
            (
                count
                for price, count in trades_by_ticker_minute.get(
                    (ticker, execution_at), []
                )
                if (
                    (action == "buy" and price <= quote)
                    or (action == "sell" and price >= quote)
                )
            ),
            start=Decimal("0.00"),
        )
        capacity_contracts = _floor_contracts(
            compatible_trade_volume * max_volume_participation
        )
        if capacity_contracts < requested_contracts:
            insufficient += 1
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
                compatible_trade_volume=compatible_trade_volume,
                capacity_contracts=capacity_contracts,
                insufficient_capacity_minutes=insufficient,
            ),
            missing,
            insufficient,
        )
    return None, missing, insufficient


def _index_trades(
    trades: list[dict[str, Any]],
) -> dict[tuple[str, datetime], list[tuple[Decimal, Decimal]]]:
    indexed: defaultdict[
        tuple[str, datetime], list[tuple[Decimal, Decimal]]
    ] = defaultdict(list)
    for trade in trades:
        created_at = trade.get("created_time_utc")
        if not isinstance(created_at, datetime):
            continue
        price = _decimal(trade.get("yes_price_dollars"))
        count = _decimal(trade.get("count_fp"))
        ticker = str(trade.get("ticker", ""))
        if price is None or count is None or count <= 0 or not ticker:
            continue
        minute_start = created_at.replace(second=0, microsecond=0)
        indexed[(ticker, minute_start)].append((price, count))
    return dict(indexed)


def _floor_contracts(value: Decimal) -> Decimal:
    units = (value / CONTRACT_QUANTUM).to_integral_value(rounding=ROUND_DOWN)
    return (units * CONTRACT_QUANTUM).quantize(CONTRACT_QUANTUM)


def _scenario_records(
    *,
    plan: TradePlan,
    config: ExecutionConfig,
    status: str,
    entry: QuoteFill | None,
    exit_fill: QuoteFill | None,
    entry_missing: int,
    exit_missing: int,
    entry_insufficient: int,
    exit_insufficient: int,
    effective_exit_available_at: datetime | None,
) -> list[dict[str, Any]]:
    return [
        _execution_record(
            plan=plan,
            config=config,
            scenario=scenario,
            status=status,
            entry=entry,
            exit_fill=exit_fill,
            entry_missing=entry_missing,
            exit_missing=exit_missing,
            entry_insufficient=entry_insufficient,
            exit_insufficient=exit_insufficient,
            effective_exit_available_at=effective_exit_available_at,
        )
        for scenario in ("base", "adverse_1cent")
    ]


def _execution_record(
    *,
    plan: TradePlan,
    config: ExecutionConfig,
    scenario: str,
    status: str,
    entry: QuoteFill | None,
    exit_fill: QuoteFill | None,
    entry_missing: int,
    exit_missing: int,
    entry_insufficient: int,
    exit_insufficient: int,
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
    requested_contracts = config.contracts_per_trade
    entry_contracts = requested_contracts if entry is not None else Decimal("0.00")
    exit_contracts = (
        requested_contracts if exit_fill is not None else Decimal("0.00")
    )
    entry_notional = (
        (entry_price * entry_contracts).quantize(MONEY_QUANTUM)
        if entry_price is not None
        else None
    )
    exit_notional = (
        (exit_price * exit_contracts).quantize(MONEY_QUANTUM)
        if exit_price is not None
        else None
    )
    entry_fee = (
        kalshi_taker_fee(
            price=entry_price,
            contracts=entry_contracts,
            rate=config.taker_fee_rate,
            multiplier=config.fee_multiplier,
            rounding_quantum=config.fee_rounding_quantum,
        )
        if entry_price is not None
        else None
    )
    exit_fee = (
        kalshi_taker_fee(
            price=exit_price,
            contracts=exit_contracts,
            rate=config.taker_fee_rate,
            multiplier=config.fee_multiplier,
            rounding_quantum=config.fee_rounding_quantum,
        )
        if exit_price is not None
        else None
    )
    gross_pnl = (
        ((exit_price - entry_price) * requested_contracts).quantize(MONEY_QUANTUM)
        if entry_price is not None and exit_price is not None
        else None
    )
    total_fees = (
        ((entry_fee or Decimal(0)) + (exit_fee or Decimal(0))).quantize(
            MONEY_QUANTUM
        )
        if entry_fee is not None or exit_fee is not None
        else Decimal("0.0000")
    )
    pnl = (
        (gross_pnl - total_fees).quantize(MONEY_QUANTUM)
        if gross_pnl is not None
        else None
    )
    entry_cost = (
        (entry_notional + entry_fee).quantize(MONEY_QUANTUM)
        if entry_notional is not None and entry_fee is not None
        else None
    )
    return_on_cost = (
        (pnl / entry_cost).quantize(RETURN_QUANTUM, rounding=ROUND_HALF_UP)
        if (
            pnl is not None
            and isinstance(entry_cost, Decimal)
            and entry_cost != Decimal(0)
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
        "unfilled_reason": _unfilled_reason(
            status=status,
            entry_missing=entry_missing,
            exit_missing=exit_missing,
            entry_insufficient=entry_insufficient,
            exit_insufficient=exit_insufficient,
        ),
        "requested_contracts": requested_contracts,
        "entry_filled_contracts": entry_contracts,
        "exit_filled_contracts": exit_contracts,
        "entry_signal_after_utc": plan.entry_signal_after_utc,
        "entry_execution_at_utc": entry.execution_at_utc if entry else None,
        "entry_delay_seconds": entry.delay_seconds if entry else None,
        "missing_entry_quote_minutes": entry_missing,
        "insufficient_entry_capacity_minutes": entry_insufficient,
        "entry_compatible_trade_volume": (
            entry.compatible_trade_volume if entry else None
        ),
        "entry_capacity_contracts": entry.capacity_contracts if entry else None,
        "entry_raw_ask": entry.price if entry else None,
        "entry_bid": entry.bid if entry else None,
        "entry_spread": entry.spread if entry else None,
        "entry_price": entry_price,
        "entry_notional_dollars": entry_notional,
        "entry_fee_dollars": entry_fee,
        "exit_signal_after_utc": plan.exit_signal_after_utc,
        "effective_exit_available_at_utc": effective_exit_available_at,
        "exit_execution_at_utc": exit_fill.execution_at_utc if exit_fill else None,
        "exit_delay_seconds": exit_fill.delay_seconds if exit_fill else None,
        "missing_exit_quote_minutes": exit_missing,
        "insufficient_exit_capacity_minutes": exit_insufficient,
        "exit_compatible_trade_volume": (
            exit_fill.compatible_trade_volume if exit_fill else None
        ),
        "exit_capacity_contracts": (
            exit_fill.capacity_contracts if exit_fill else None
        ),
        "exit_raw_bid": exit_fill.price if exit_fill else None,
        "exit_ask": exit_fill.ask if exit_fill else None,
        "exit_spread": exit_fill.spread if exit_fill else None,
        "exit_price": exit_price,
        "exit_notional_dollars": exit_notional,
        "exit_fee_dollars": exit_fee,
        "holding_seconds": (
            int(
                (exit_fill.execution_at_utc - entry.execution_at_utc).total_seconds()
            )
            if entry is not None and exit_fill is not None
            else None
        ),
        "gross_pnl_dollars": gross_pnl,
        "total_fees_dollars": total_fees,
        "pnl_dollars": pnl,
        "return_on_cost": return_on_cost,
        "price_rule": "next_minute_open_quote",
        "capacity_rule": (
            "all_or_none_at_or_better_trade_volume_participation"
        ),
        "fee_rule": "kalshi_quadratic_taker_fee_rounded_up",
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


def kalshi_taker_fee(
    *,
    price: Decimal,
    contracts: Decimal,
    rate: Decimal,
    multiplier: Decimal,
    rounding_quantum: Decimal,
) -> Decimal:
    """Calculate the quadratic fee for one all-or-none taker fill."""

    if not Decimal(0) <= price <= Decimal(1):
        raise ValueError("price must be between 0 and 1")
    if contracts < 0:
        raise ValueError("contracts cannot be negative")
    raw_fee = multiplier * rate * contracts * price * (Decimal(1) - price)
    if raw_fee == 0:
        return Decimal("0.0000")
    rounded_units = (raw_fee / rounding_quantum).to_integral_value(
        rounding=ROUND_CEILING
    )
    return (rounded_units * rounding_quantum).quantize(MONEY_QUANTUM)


def _unfilled_reason(
    *,
    status: str,
    entry_missing: int,
    exit_missing: int,
    entry_insufficient: int,
    exit_insufficient: int,
) -> str | None:
    if status == "filled":
        return None
    if status == "entry_unfilled":
        return _search_failure_reason(
            leg="entry",
            missing=entry_missing,
            insufficient=entry_insufficient,
        )
    return _search_failure_reason(
        leg="exit",
        missing=exit_missing,
        insufficient=exit_insufficient,
    )


def _search_failure_reason(*, leg: str, missing: int, insufficient: int) -> str:
    if insufficient and missing:
        return f"{leg}_quotes_missing_and_capacity_insufficient"
    if insufficient:
        return f"{leg}_capacity_insufficient"
    return f"{leg}_quotes_missing"


def _decimal(value: object) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (ArithmeticError, ValueError):
        return None


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
