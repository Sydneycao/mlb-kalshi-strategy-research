from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq  # type: ignore[import-untyped]

from mlb_kalshi.backfill import HistoricalBackfillPipeline
from mlb_kalshi.config import Settings
from mlb_kalshi.normalize import build_kalshi_game
from mlb_kalshi.research.pipeline import resolve_input_manifest
from mlb_kalshi.storage import RawStore


class FakeKalshi:
    def __init__(self, games: list[Any]) -> None:
        self.games = games
        self.discovery_calls = 0
        self.candle_tickers: list[str] = []
        self.trade_tickers: list[str] = []

    def get_cutoff(self, *, raw_section: str) -> dict[str, str]:
        return {
            "market_settled_ts": "2025-01-01T00:00:00Z",
            "trades_created_ts": "2025-01-01T00:00:00Z",
            "orders_updated_ts": "2025-01-01T00:00:00Z",
        }

    def discover_games_in_range(self, **_: Any) -> tuple[list[Any], dict[str, Any]]:
        self.discovery_calls += 1
        return self.games, {
            "current": {"truncated": False},
            "historical": {"truncated": False},
            "truncated_sources": [],
            "selected": {"total": len(self.games)},
        }

    def get_candlesticks(self, *, market: dict[str, Any], **_: Any) -> dict[str, Any]:
        ticker = str(market["ticker"])
        self.candle_tickers.append(ticker)
        return {
            "candlesticks": [
                {
                    "end_period_ts": 1_752_341_460,
                    "volume_fp": "10.00",
                    "open_interest_fp": "2.00",
                    "yes_bid": {"open_dollars": "0.48"},
                    "yes_ask": {"open_dollars": "0.52"},
                    "price": {"open_dollars": "0.50"},
                }
            ]
        }

    def get_trades(
        self, *, ticker: str, source: str, **_: Any
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        self.trade_tickers.append(ticker)
        return (
            [
                {
                    "trade_id": f"{ticker}-trade",
                    "ticker": ticker,
                    "created_time": "2025-07-12T17:11:00Z",
                    "count_fp": "3.00",
                    "yes_price_dollars": "0.50",
                    "no_price_dollars": "0.50",
                    "taker_side": "yes",
                }
            ],
            {
                "path": f"/{source}/trades",
                "pages": 1,
                "items": 1,
                "truncated": False,
            },
        )


class FakeMlb:
    def __init__(self) -> None:
        self.schedule_calls = 0
        self.play_calls: list[int] = []

    def get_schedule(self, **_: Any) -> dict[str, Any]:
        self.schedule_calls += 1
        return _schedule_payload()

    def get_play_by_play(self, game_pk: int) -> dict[str, Any]:
        self.play_calls.append(game_pk)
        return {
            "liveData": {
                "plays": {
                    "allPlays": [
                        {
                            "about": {
                                "atBatIndex": 0,
                                "inning": 1,
                                "halfInning": "top",
                                "startTime": "2025-07-12T17:11:00Z",
                                "endTime": "2025-07-12T17:12:00Z",
                                "isComplete": True,
                            },
                            "result": {
                                "event": "Single",
                                "description": "A test play.",
                                "awayScore": 0,
                                "homeScore": 0,
                            },
                        }
                    ]
                }
            }
        }


class FailingCandleKalshi(FakeKalshi):
    def get_candlesticks(self, *, market: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError(f"temporary candle failure for {market['ticker']}")


class FakeContext:
    def __init__(self, kalshi: FakeKalshi, mlb: FakeMlb) -> None:
        self.kalshi = kalshi
        self.mlb = mlb

    def __enter__(self) -> tuple[Any, Any]:
        return self.kalshi, self.mlb

    def __exit__(self, *_: object) -> None:
        return None


class FakeBackfillPipeline(HistoricalBackfillPipeline):
    def __init__(
        self, settings: Settings, kalshi: FakeKalshi, mlb: FakeMlb
    ) -> None:
        super().__init__(settings)
        self.context = FakeContext(kalshi, mlb)

    def _clients(self, raw: RawStore) -> Any:
        return self.context


def test_backfill_resumes_without_redownloading_completed_games(
    tmp_path: Path,
) -> None:
    games = _market_games()
    first_kalshi = FakeKalshi(games)
    first_mlb = FakeMlb()
    settings = Settings(output_dir=tmp_path)

    first = FakeBackfillPipeline(settings, first_kalshi, first_mlb).run(
        job_id="2025-season",
        start_date=date(2025, 7, 12),
        end_date=date(2025, 7, 13),
        max_games=2,
        batch_size=1,
        max_games_this_run=1,
    )

    assert first["status"] == "partial"
    assert first["counts"]["completed_games"] == 1
    assert first_kalshi.discovery_calls == 1
    assert len(first_kalshi.candle_tickers) == 2
    assert first_mlb.schedule_calls == 1

    second_kalshi = FakeKalshi(games)
    second_mlb = FakeMlb()
    second = FakeBackfillPipeline(settings, second_kalshi, second_mlb).run(
        job_id="2025-season",
        start_date=None,
        end_date=None,
        max_games=None,
        batch_size=None,
    )

    assert second["resumed"]
    assert second["status"] == "completed"
    assert second["counts"]["completed_games"] == 2
    assert second["counts"]["matched_games"] == 2
    assert second["counts"]["kalshi_markets"] == 4
    assert second_kalshi.discovery_calls == 0
    assert second_mlb.schedule_calls == 0
    assert len(second_kalshi.candle_tickers) == 2

    state = json.loads(
        (tmp_path / "runs" / "backfill_2025-season" / "state.json").read_text()
    )
    assert {
        checkpoint["attempts"] for checkpoint in state["games"].values()
    } == {1}
    markets = pq.read_table(
        tmp_path
        / "normalized"
        / "backfill_2025-season"
        / "kalshi_markets.parquet"
    )
    assert markets.num_rows == 4


def test_resume_rejects_configuration_changes(tmp_path: Path) -> None:
    games = _market_games()[:1]
    settings = Settings(output_dir=tmp_path)
    pipeline = FakeBackfillPipeline(settings, FakeKalshi(games), FakeMlb())
    pipeline.run(
        job_id="fixed",
        start_date=date(2025, 7, 12),
        end_date=date(2025, 7, 12),
        max_games=1,
        batch_size=1,
    )

    try:
        pipeline.run(
            job_id="fixed",
            start_date=None,
            end_date=None,
            max_games=500,
            batch_size=None,
        )
    except ValueError as exc:
        assert "max_games" in str(exc)
    else:
        raise AssertionError("resume accepted a conflicting max_games")


def test_failed_game_is_retried_on_next_invocation(tmp_path: Path) -> None:
    games = _market_games()[:1]
    settings = Settings(output_dir=tmp_path)

    first = FakeBackfillPipeline(
        settings, FailingCandleKalshi(games), FakeMlb()
    ).run(
        job_id="retry",
        start_date=date(2025, 7, 12),
        end_date=date(2025, 7, 12),
        max_games=1,
        batch_size=1,
    )
    assert first["status"] == "needs_retry"
    assert first["counts"]["failed_games"] == 1

    second_kalshi = FakeKalshi(games)
    second_mlb = FakeMlb()
    second = FakeBackfillPipeline(settings, second_kalshi, second_mlb).run(
        job_id="retry",
        start_date=None,
        end_date=None,
        max_games=None,
        batch_size=None,
    )
    assert second["status"] == "completed"
    assert second["counts"]["failed_games"] == 0
    assert second_kalshi.discovery_calls == 0
    assert second_mlb.schedule_calls == 0

    state = json.loads(
        (tmp_path / "runs" / "backfill_retry" / "state.json").read_text()
    )
    checkpoint = next(iter(state["games"].values()))
    assert checkpoint["attempts"] == 2
    assert checkpoint["status"] == "completed"


def test_latest_input_prefers_completed_backfill_and_skips_partial(
    tmp_path: Path,
) -> None:
    runs = tmp_path / "runs"
    smoke = runs / "smoke_old" / "manifest.json"
    completed = runs / "backfill_complete" / "manifest.json"
    partial = runs / "backfill_partial" / "manifest.json"
    for path, payload in (
        (smoke, {"run_type": "smoke"}),
        (completed, {"run_type": "backfill", "status": "completed"}),
        (partial, {"run_type": "backfill", "status": "partial"}),
    ):
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(payload), encoding="utf-8")
    os.utime(smoke, ns=(1, 1))
    os.utime(completed, ns=(2, 2))
    os.utime(partial, ns=(3, 3))

    assert resolve_input_manifest(tmp_path, None) == completed


def _market_games() -> list[Any]:
    return [
        _market_game("KXMLBGAME-25JUL121310BOSNYY", "2025-07-12T17:10:00Z"),
        _market_game("KXMLBGAME-25JUL131310BOSNYY", "2025-07-13T17:10:00Z"),
    ]


def _market_game(event_ticker: str, occurrence: str) -> Any:
    shared = {
        "event_ticker": event_ticker,
        "occurrence_datetime": occurrence,
        "open_time": occurrence,
        "close_time": occurrence,
        "settlement_ts": occurrence,
        "status": "settled",
    }
    return build_kalshi_game(
        event_ticker,
        "current",
        [
            {
                **shared,
                "ticker": f"{event_ticker}-BOS",
                "title": "Boston vs. New York Y Winner?",
                "yes_sub_title": "Boston",
                "no_sub_title": "Boston",
                "result": "yes",
            },
            {
                **shared,
                "ticker": f"{event_ticker}-NYY",
                "title": "Boston vs. New York Y Winner?",
                "yes_sub_title": "New York Y",
                "no_sub_title": "New York Y",
                "result": "no",
            },
        ],
    )


def _schedule_payload() -> dict[str, Any]:
    return {
        "dates": [
            {
                "games": [
                    _raw_game(1, "2025-07-12", "2025-07-12T17:10:00Z"),
                    _raw_game(2, "2025-07-13", "2025-07-13T17:10:00Z"),
                ]
            }
        ]
    }


def _raw_game(game_pk: int, official_date: str, game_date: str) -> dict[str, Any]:
    return {
        "gamePk": game_pk,
        "officialDate": official_date,
        "gameDate": game_date,
        "doubleHeader": "N",
        "gameNumber": 1,
        "status": {
            "abstractGameState": "Final",
            "detailedState": "Final",
        },
        "teams": {
            "away": {
                "team": {"name": "Boston Red Sox"},
                "score": 5,
            },
            "home": {
                "team": {"name": "New York Yankees"},
                "score": 3,
            },
        },
    }
