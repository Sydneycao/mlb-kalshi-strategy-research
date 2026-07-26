from datetime import UTC, datetime, timedelta
from decimal import Decimal

from mlb_kalshi.research.execution import execute_trade_plans
from mlb_kalshi.research.models import TradePlan
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


def test_next_open_quotes_missing_minutes_spreads_and_slippage() -> None:
    timeline = [
        _row(0, bid="0.3900", ask="0.4000"),
        _row(1, bid="0.4100", ask=None),
        _row(2, bid="0.4200", ask="0.4300"),
        _row(4, bid=None, ask="0.5000"),
        _row(5, bid="0.4800", ask="0.4900"),
    ]

    executions = execute_trade_plans([_plan()], timeline)
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
    assert base["pnl_dollars"] == Decimal("0.0500")

    assert adverse["entry_price"] == Decimal("0.4400")
    assert adverse["exit_price"] == Decimal("0.4700")
    assert adverse["pnl_dollars"] == Decimal("0.0300")
    validate_no_lookahead([], executions)


def test_same_minute_quote_and_extrema_are_not_executable() -> None:
    timeline = [
        _row(0, bid="0.1000", ask="0.1100", forbidden_low="0.0100"),
        _row(1, bid="0.2000", ask="0.2500", forbidden_low="0.0200"),
        _row(4, bid="0.7000", ask="0.8000", forbidden_high="0.9900"),
    ]

    executions = execute_trade_plans([_plan()], timeline)
    base = next(row for row in executions if row["scenario"] == "base")

    assert base["entry_raw_ask"] == Decimal("0.2500")
    assert base["exit_raw_bid"] == Decimal("0.7000")
    assert base["entry_execution_at_utc"] >= _plan().entry_signal_after_utc
    assert base["exit_execution_at_utc"] >= _plan().exit_signal_after_utc


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
