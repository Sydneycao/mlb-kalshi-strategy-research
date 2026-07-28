from datetime import UTC, datetime, timedelta
from decimal import Decimal

from mlb_kalshi.research.execution import execute_trade_plans, kalshi_taker_fee
from mlb_kalshi.research.models import ExecutionConfig, TradePlan
from mlb_kalshi.research.pipeline import validate_no_lookahead


def _time(minute: int) -> datetime:
    return datetime(2026, 7, 1, 12, minute, tzinfo=UTC)


def _row(
    minute: int,
    *,
    bid: str | None,
    ask: str | None,
    forbidden_high: str = "0.9900",
    forbidden_low: str = "0.0100",
) -> dict[str, object]:
    return {
        "ticker": "TEST-BOS",
        "minute_start_utc": _time(minute),
        "yes_bid_open": Decimal(bid) if bid else None,
        "yes_ask_open": Decimal(ask) if ask else None,
        # Execution must never consult these extrema.
        "yes_bid_high": Decimal(forbidden_high),
        "yes_ask_low": Decimal(forbidden_low),
    }


def _plan() -> TradePlan:
    return TradePlan(
        plan_id="test-plan",
        strategy="test",
        event_ticker="TEST",
        ticker="TEST-BOS",
        entry_signal_minute_start_utc=_time(0),
        entry_signal_after_utc=_time(1),
        exit_signal_minute_start_utc=_time(3),
        exit_signal_after_utc=_time(4),
        entry_reason="observed during minute zero",
        exit_reason="observed during minute three",
        parameters={},
    )


def _trade(minute: int, *, price: str, count: str) -> dict[str, object]:
    return {
        "ticker": "TEST-BOS",
        "created_time_utc": _time(minute) + timedelta(seconds=10),
        "yes_price_dollars": price,
        "count_fp": count,
    }


def _config(contracts: str = "1.00") -> ExecutionConfig:
    return ExecutionConfig(
        contracts_per_trade=Decimal(contracts),
        max_volume_participation=Decimal("0.10"),
    )


def test_next_open_quotes_missing_minutes_spreads_and_slippage() -> None:
    timeline = [
        _row(0, bid="0.3900", ask="0.4000"),
        _row(1, bid="0.4100", ask=None),
        _row(2, bid="0.4200", ask="0.4300"),
        _row(4, bid=None, ask="0.5000"),
        _row(5, bid="0.4800", ask="0.4900"),
    ]

    executions = execute_trade_plans(
        [_plan()],
        timeline,
        [
            _trade(2, price="0.4300", count="20.00"),
            _trade(5, price="0.4800", count="20.00"),
        ],
        _config(),
    )
    base = next(row for row in executions if row["scenario"] == "base")
    adverse = next(
        row for row in executions if row["scenario"] == "adverse_1cent"
    )

    assert base["entry_execution_at_utc"] == _time(2)
    assert base["entry_raw_ask"] == Decimal("0.4300")
    assert base["entry_delay_seconds"] == 60
    assert base["missing_entry_quote_minutes"] == 1
    assert base["entry_spread"] == Decimal("0.0100")
    assert base["exit_execution_at_utc"] == _time(5)
    assert base["exit_raw_bid"] == Decimal("0.4800")
    assert base["exit_delay_seconds"] == 60
    assert base["missing_exit_quote_minutes"] == 1
    assert base["exit_spread"] == Decimal("0.0100")
    assert base["gross_pnl_dollars"] == Decimal("0.0500")
    assert base["entry_fee_dollars"] == Decimal("0.0200")
    assert base["exit_fee_dollars"] == Decimal("0.0200")
    assert base["total_fees_dollars"] == Decimal("0.0400")
    assert base["pnl_dollars"] == Decimal("0.0100")
    assert base["requested_contracts"] == Decimal("1.00")
    assert base["entry_capacity_contracts"] == Decimal("2.00")
    assert base["exit_capacity_contracts"] == Decimal("2.00")

    assert adverse["entry_price"] == Decimal("0.4400")
    assert adverse["exit_price"] == Decimal("0.4700")
    assert adverse["gross_pnl_dollars"] == Decimal("0.0300")
    assert adverse["pnl_dollars"] == Decimal("-0.0100")
    validate_no_lookahead([], executions)


def test_same_minute_quote_and_extrema_are_not_executable() -> None:
    timeline = [
        _row(0, bid="0.1000", ask="0.1100", forbidden_low="0.0100"),
        _row(1, bid="0.2000", ask="0.2500", forbidden_low="0.0200"),
        _row(4, bid="0.7000", ask="0.8000", forbidden_high="0.9900"),
    ]

    executions = execute_trade_plans(
        [_plan()],
        timeline,
        [
            _trade(1, price="0.2500", count="20.00"),
            _trade(4, price="0.7000", count="20.00"),
        ],
        _config(),
    )
    base = next(row for row in executions if row["scenario"] == "base")

    assert base["entry_raw_ask"] == Decimal("0.2500")
    assert base["exit_raw_bid"] == Decimal("0.7000")
    assert base["entry_execution_at_utc"] >= _plan().entry_signal_after_utc
    assert base["exit_execution_at_utc"] >= _plan().exit_signal_after_utc


def test_insufficient_trade_capacity_delays_all_or_none_fill() -> None:
    timeline = [
        _row(1, bid="0.3900", ask="0.4000"),
        _row(2, bid="0.4100", ask="0.4200"),
        _row(4, bid="0.5000", ask="0.5100"),
    ]
    trades = [
        _trade(1, price="0.4000", count="5.00"),
        _trade(2, price="0.4200", count="20.00"),
        _trade(4, price="0.5000", count="20.00"),
    ]

    base = next(
        row
        for row in execute_trade_plans(
            [_plan()], timeline, trades, _config()
        )
        if row["scenario"] == "base"
    )

    assert base["entry_execution_at_utc"] == _time(2)
    assert base["entry_delay_seconds"] == 60
    assert base["insufficient_entry_capacity_minutes"] == 1
    assert base["entry_compatible_trade_volume"] == Decimal("20.00")
    assert base["entry_capacity_contracts"] == Decimal("2.00")
    assert base["status"] == "filled"


def test_no_trade_capacity_leaves_entry_unfilled() -> None:
    executions = execute_trade_plans(
        [_plan()],
        [
            _row(1, bid="0.3900", ask="0.4000"),
            _row(2, bid="0.4100", ask="0.4200"),
        ],
        [],
        _config(),
    )
    base = next(row for row in executions if row["scenario"] == "base")

    assert base["status"] == "entry_unfilled"
    assert base["unfilled_reason"] == "entry_capacity_insufficient"
    assert base["insufficient_entry_capacity_minutes"] == 2
    assert base["entry_filled_contracts"] == Decimal("0.00")
    assert base["pnl_dollars"] is None


def test_contract_quantity_and_quadratic_fees_scale_pnl() -> None:
    timeline = [
        _row(1, bid="0.4900", ask="0.5000"),
        _row(4, bid="0.6000", ask="0.6100"),
    ]
    trades = [
        _trade(1, price="0.5000", count="200.00"),
        _trade(4, price="0.6000", count="200.00"),
    ]
    base = next(
        row
        for row in execute_trade_plans(
            [_plan()], timeline, trades, _config("10.00")
        )
        if row["scenario"] == "base"
    )

    assert base["entry_notional_dollars"] == Decimal("5.0000")
    assert base["exit_notional_dollars"] == Decimal("6.0000")
    assert base["gross_pnl_dollars"] == Decimal("1.0000")
    assert base["entry_fee_dollars"] == Decimal("0.1800")
    assert base["exit_fee_dollars"] == Decimal("0.1700")
    assert base["total_fees_dollars"] == Decimal("0.3500")
    assert base["pnl_dollars"] == Decimal("0.6500")
    assert base["return_on_cost"] == Decimal("0.125483")


def test_fee_rounds_up_once_per_leg() -> None:
    assert kalshi_taker_fee(
        price=Decimal("0.5000"),
        contracts=Decimal("1.00"),
        rate=Decimal("0.0700"),
        multiplier=Decimal("1.0000"),
        rounding_quantum=Decimal("0.0100"),
    ) == Decimal("0.0200")


def test_signal_is_available_exactly_after_its_minute_closes() -> None:
    plan = _plan()
    signals = [
        {
            "observed_minute_start_utc": plan.entry_signal_minute_start_utc,
            "signal_available_at_utc": plan.entry_signal_after_utc,
        },
        {
            "observed_minute_start_utc": plan.exit_signal_minute_start_utc,
            "signal_available_at_utc": plan.exit_signal_after_utc,
        },
    ]
    validate_no_lookahead(signals, [])
    assert plan.entry_signal_after_utc == plan.entry_signal_minute_start_utc + timedelta(
        minutes=1
    )
