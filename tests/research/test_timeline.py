import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from mlb_kalshi.research.timeline import build_minute_timeline


def _time(minute: int, second: int = 0) -> datetime:
    return datetime(2026, 7, 1, 12, 0, second, tzinfo=UTC) + timedelta(
        minutes=minute
    )


def test_play_state_is_not_visible_until_observation_minute_closes(
    tmp_path: Path,
) -> None:
    raw_dir = tmp_path / "raw"
    pbp_dir = raw_dir / "mlb" / "play_by_play"
    pbp_dir.mkdir(parents=True)
    payload = {
        "liveData": {
            "plays": {
                "allPlays": [
                    {
                        "about": {
                            "atBatIndex": 0,
                            "endTime": "2026-07-01T12:00:30Z",
                            "halfInning": "top",
                            "inning": 1,
                        },
                        "count": {"outs": 0},
                        "result": {
                            "awayScore": 1,
                            "homeScore": 0,
                            "event": "Home Run",
                            "description": "Boston scores.",
                        },
                        "runners": [],
                        "playEvents": [],
                    }
                ]
            }
        }
    }
    (pbp_dir / "game_1.json").write_text(json.dumps(payload), encoding="utf-8")

    matches = [
        {
            "event_ticker": "TEST",
            "game_pk": 1,
            "mlb_start_utc": _time(0),
            "away_team": "BOS",
            "home_team": "NYY",
        }
    ]
    markets = [
        {
            "event_ticker": "TEST",
            "ticker": "TEST-BOS",
            "source": "historical",
            "yes_team": "Boston",
            "open_time_utc": _time(-1),
            "settlement_time_utc": _time(2),
        }
    ]
    candles = [
        {
            "event_ticker": "TEST",
            "ticker": "TEST-BOS",
            "end_period_time_utc": _time(1),
            "yes_bid_open_dollars": "0.5000",
            "yes_ask_open_dollars": "0.5200",
            "yes_bid_close_dollars": "0.6000",
            "yes_ask_close_dollars": "0.6200",
            "price_close_dollars": "0.6100",
            "volume_fp": "1.00",
            "open_interest_fp": "1.00",
        }
    ]

    rows = build_minute_timeline(
        matches=matches,
        markets=markets,
        candles=candles,
        raw_dir=raw_dir,
        pregame_minutes=1,
    )

    previous = next(row for row in rows if row["minute_start_utc"] == _time(-1))
    first = next(row for row in rows if row["minute_start_utc"] == _time(0))
    assert previous["minute_end_utc"] == _time(0)
    assert previous["away_score"] == 0
    assert first["minute_end_utc"] == _time(1)
    assert first["away_score"] == 1
    assert first["yes_runs_scored_this_minute"] == 1
    assert first["observed_play_count"] == 1
    assert first["quote_observed"]

    assert all(
        row["away_score"] == 0
        for row in rows
        if row["minute_end_utc"] <= _time(0)
    )


def test_missing_candle_is_an_explicit_missing_quote_minute(tmp_path: Path) -> None:
    raw_dir = tmp_path / "raw"
    pbp_dir = raw_dir / "mlb" / "play_by_play"
    pbp_dir.mkdir(parents=True)
    payload = {
        "liveData": {
            "plays": {
                "allPlays": [
                    {
                        "about": {
                            "atBatIndex": 0,
                            "endTime": "2026-07-01T12:00:30Z",
                            "halfInning": "top",
                            "inning": 1,
                        },
                        "count": {"outs": 1},
                        "result": {
                            "awayScore": 0,
                            "homeScore": 0,
                            "event": "Flyout",
                            "description": "One out.",
                        },
                        "runners": [],
                        "playEvents": [],
                    }
                ]
            }
        }
    }
    (pbp_dir / "game_1.json").write_text(json.dumps(payload), encoding="utf-8")
    rows = build_minute_timeline(
        matches=[
            {
                "event_ticker": "TEST",
                "game_pk": 1,
                "mlb_start_utc": _time(0),
                "away_team": "BOS",
                "home_team": "NYY",
            }
        ],
        markets=[
            {
                "event_ticker": "TEST",
                "ticker": "TEST-NYY",
                "source": "historical",
                "yes_team": "New York Yankees",
                "open_time_utc": _time(0),
                "settlement_time_utc": _time(2),
            }
        ],
        candles=[],
        raw_dir=raw_dir,
        pregame_minutes=0,
    )
    assert len(rows) == 2
    assert all(row["quote_observed"] is False for row in rows)
    assert all(row["missing_yes_bid_open"] for row in rows)
    assert all(row["missing_yes_ask_open"] for row in rows)
