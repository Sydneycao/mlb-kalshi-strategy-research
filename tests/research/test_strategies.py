from datetime import UTC, datetime, timedelta
from decimal import Decimal

from mlb_kalshi.research.strategies import PregameToLive, ThreatResolution


def _time(minute: int) -> datetime:
    return datetime(2026, 7, 1, 12, 0, tzinfo=UTC) + timedelta(minutes=minute)


def _row(ticker: str, minute: int, bid: str, ask: str, phase: str) -> dict[str, object]:
    return {
        "event_ticker": "TEST",
        "ticker": ticker,
        "scheduled_start_utc": _time(10),
        "minute_start_utc": _time(minute),
        "minute_end_utc": _time(minute + 1),
        "yes_bid_close": Decimal(bid),
        "yes_ask_close": Decimal(ask),
        "game_phase": phase,
    }


def test_pregame_strategy_selects_favorite_but_signals_after_close() -> None:
    timeline = [
        _row("TEST-A", 4, "0.5900", "0.6100", "pregame"),
        _row("TEST-B", 4, "0.3900", "0.4100", "pregame"),
        _row("TEST-A", 24, "0.6500", "0.6700", "live"),
        _row("TEST-B", 24, "0.3300", "0.3500", "live"),
    ]

    plans = PregameToLive(entry_lead_minutes=5, live_exit_minutes=15).generate(
        timeline
    )

    assert len(plans) == 1
    assert plans[0].ticker == "TEST-A"
    assert plans[0].entry_signal_minute_start_utc == _time(4)
    assert plans[0].entry_signal_after_utc == _time(5)
    assert plans[0].exit_signal_minute_start_utc == _time(24)
    assert plans[0].exit_signal_after_utc == _time(25)


def test_threat_resolution_waits_until_risp_is_cleared_without_a_run() -> None:
    rows: list[dict[str, object]] = []
    for minute in range(12):
        rows.append(
            {
                "event_ticker": "TEST",
                "ticker": "TEST-HOME",
                "minute_start_utc": _time(minute),
                "minute_end_utc": _time(minute + 1),
                "game_phase": "live",
                "inning": 5,
                "half_inning": "top",
                "opponent_score": 0,
                "threat_against_yes": minute == 0,
                "runners_in_scoring_position": 1 if minute == 0 else 0,
                "base_runners": 1,
                "game_over_observed": False,
            }
        )

    plans = ThreatResolution(hold_minutes=10).generate(rows)

    assert len(plans) == 1
    assert plans[0].entry_signal_minute_start_utc == _time(1)
    assert plans[0].entry_signal_after_utc == _time(2)
    assert plans[0].exit_signal_after_utc == _time(12)
