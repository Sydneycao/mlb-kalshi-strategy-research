from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from datetime import UTC, date, datetime, timedelta
from functools import partial
from typing import Any, TypeVar, cast

import structlog

from mlb_kalshi.clients.kalshi import KalshiClient, market_window
from mlb_kalshi.clients.mlb import MlbClient
from mlb_kalshi.config import Settings
from mlb_kalshi.http import ResilientJsonClient
from mlb_kalshi.matching import match_game, mlb_winner
from mlb_kalshi.models import (
    KalshiGame,
    MarketSource,
    MatchedGame,
    MlbGame,
    Rejection,
)
from mlb_kalshi.normalize import (
    event_game_date,
    normalize_candlesticks,
    normalize_mlb_schedule,
    normalize_plays,
    normalize_trades,
)
from mlb_kalshi.storage import (
    CANDLE_SCHEMA,
    MARKET_SCHEMA,
    MATCH_SCHEMA,
    MLB_GAME_SCHEMA,
    PLAY_SCHEMA,
    REJECTION_SCHEMA,
    TRADE_SCHEMA,
    NormalizedStore,
    RawStore,
    RunLayout,
    new_run_id,
    write_manifest,
)
from mlb_kalshi.time import optional_utc, parse_utc, utc_iso

T = TypeVar("T")


class ResearchPipeline:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._log = structlog.get_logger("pipeline")

    def probe(self) -> dict[str, Any]:
        layout = RunLayout(self.settings.output_dir, new_run_id("probe"))
        layout.create()
        raw = RawStore(layout.raw_dir)
        checks: list[dict[str, Any]] = []

        with self._clients(raw) as (kalshi, mlb):
            cutoff = self._probe(
                checks, "kalshi_historical_cutoff", kalshi.get_cutoff
            )
            current_result = self._probe(
                checks,
                "kalshi_current_markets",
                lambda: kalshi.list_settled_markets(
                    "current",
                    max_pages=1,
                    target_events=1,
                    raw_section="probe_current_markets",
                    page_size=10,
                ),
            )
            historical_result = self._probe(
                checks,
                "kalshi_historical_markets",
                lambda: kalshi.list_settled_markets(
                    "historical",
                    max_pages=1,
                    target_events=1,
                    raw_section="probe_historical_markets",
                    page_size=10,
                ),
            )

            current_market = _first_market(current_result)
            historical_market = _first_market(historical_result)
            for source, market in (
                ("current", current_market),
                ("historical", historical_market),
            ):
                if market is None:
                    checks.append(
                        {
                            "name": f"kalshi_{source}_candlesticks",
                            "status": "skipped",
                            "detail": "no settled sample market returned",
                        }
                    )
                    checks.append(
                        {
                            "name": f"kalshi_{source}_trades",
                            "status": "skipped",
                            "detail": "no settled sample market returned",
                        }
                    )
                    continue
                window = market_window(market)
                if window is None:
                    checks.append(
                        {
                            "name": f"kalshi_{source}_candlesticks",
                            "status": "failed",
                            "detail": "sample market has no valid data window",
                        }
                    )
                    continue
                start, end = window
                probe_start = max(start, end - timedelta(hours=1))
                self._probe(
                    checks,
                    f"kalshi_{source}_candlesticks",
                    partial(
                        kalshi.get_candlesticks,
                        market=market,
                        source=cast(MarketSource, source),
                        start=probe_start,
                        end=end,
                        raw_section="probe",
                    ),
                )
                self._probe(
                    checks,
                    f"kalshi_{source}_trades",
                    partial(
                        kalshi.get_trades,
                        ticker=str(market["ticker"]),
                        source=cast(Any, source),
                        start=probe_start,
                        end=end,
                        max_pages=1,
                        raw_section="probe",
                        page_size=10,
                    ),
                )

            schedule_date = _sample_game_date(historical_market or current_market)
            schedule = self._probe(
                checks,
                "mlb_schedule",
                lambda: mlb.get_schedule(
                    start_date=schedule_date,
                    end_date=schedule_date,
                    raw_section="probe_schedule",
                ),
            )
            game_pk = (
                mlb.first_game_pk(schedule) if isinstance(schedule, dict) else None
            )
            if game_pk is None:
                checks.append(
                    {
                        "name": "mlb_play_by_play",
                        "status": "skipped",
                        "detail": "schedule returned no sample game",
                    }
                )
            else:
                self._probe(
                    checks,
                    "mlb_play_by_play",
                    lambda: mlb.get_play_by_play(game_pk, raw_section="probe_pbp"),
                )

        manifest = {
            "run_id": layout.run_id,
            "run_type": "probe",
            "created_at_utc": utc_iso(datetime.now(UTC)),
            "checks": checks,
            "cutoff": cutoff if isinstance(cutoff, dict) else None,
            "raw_dir": str(layout.raw_dir.resolve()),
        }
        manifest_path = layout.run_dir / "manifest.json"
        write_manifest(manifest_path, manifest)
        return {
            **manifest,
            "manifest": str(manifest_path.resolve()),
            "failed": sum(check["status"] == "failed" for check in checks),
            "available": sum(check["status"] == "available" for check in checks),
        }

    def smoke(self) -> dict[str, Any]:
        layout = RunLayout(self.settings.output_dir, new_run_id("smoke"))
        layout.create()
        raw = RawStore(layout.raw_dir)
        normalized = NormalizedStore(layout.normalized_dir)
        errors: list[dict[str, Any]] = []
        pagination: dict[str, Any] = {}

        with self._clients(raw) as (kalshi, mlb):
            cutoff = kalshi.get_cutoff()
            market_games, discovery = kalshi.discover_games(
                max_games=self.settings.max_games,
                max_pages=self.settings.max_discovery_pages,
            )
            pagination["market_discovery"] = discovery

            mlb_schedule_payload: dict[str, Any] = {"dates": []}
            game_dates = [game.game_date for game in market_games if game.game_date]
            if game_dates:
                start_date = min(game_dates) - timedelta(
                    days=self.settings.schedule_buffer_days
                )
                end_date = max(game_dates) + timedelta(
                    days=self.settings.schedule_buffer_days
                )
                mlb_schedule_payload = mlb.get_schedule(
                    start_date=start_date,
                    end_date=end_date,
                )
            mlb_games = normalize_mlb_schedule(mlb_schedule_payload)

            matches: list[MatchedGame] = []
            rejections: list[Rejection] = []
            tolerance = timedelta(minutes=self.settings.match_tolerance_minutes)
            for market_game in market_games:
                result = match_game(market_game, mlb_games, tolerance=tolerance)
                if isinstance(result, MatchedGame):
                    matches.append(result)
                else:
                    rejections.append(result)
                    self._log.warning(
                        "game_match_rejected",
                        event_ticker=result.event_ticker,
                        reason_code=result.reason_code,
                        reason=result.reason,
                        details=result.details,
                    )

            market_rows = [
                _market_row(game, market)
                for game in market_games
                for market in game.markets
            ]
            candle_rows: list[dict[str, Any]] = []
            trade_rows: list[dict[str, Any]] = []
            trade_cutoff = parse_utc(str(cutoff["trades_created_ts"]))

            for game in market_games:
                for market in game.markets:
                    ticker = str(market.get("ticker", ""))
                    window = market_window(market)
                    if not ticker or window is None:
                        errors.append(
                            {
                                "resource": "kalshi_market_data",
                                "event_ticker": game.event_ticker,
                                "ticker": ticker or None,
                                "error": "market has no ticker or valid open/settlement window",
                            }
                        )
                        continue
                    start, end = window
                    try:
                        candle_payload = kalshi.get_candlesticks(
                            market=market,
                            source=game.source,
                            start=start,
                            end=end,
                            raw_section="smoke",
                        )
                        candle_rows.extend(
                            normalize_candlesticks(
                                event_ticker=game.event_ticker,
                                market_ticker=ticker,
                                source=game.source,
                                payload=candle_payload,
                            )
                        )
                    except Exception as exc:
                        self._record_resource_error(
                            errors, "candlesticks", game, ticker, exc
                        )

                    segments: list[tuple[str, datetime, datetime]] = []
                    if start <= trade_cutoff:
                        segments.append(
                            ("historical", start, min(end, trade_cutoff))
                        )
                    if end >= trade_cutoff:
                        segments.append(("current", max(start, trade_cutoff), end))
                    for trade_source, segment_start, segment_end in segments:
                        if segment_end < segment_start:
                            continue
                        try:
                            trades, metadata = kalshi.get_trades(
                                ticker=ticker,
                                source=trade_source,  # type: ignore[arg-type]
                                start=segment_start,
                                end=segment_end,
                                max_pages=self.settings.max_trade_pages,
                                raw_section="smoke",
                            )
                            pagination[
                                f"trades:{trade_source}:{ticker}"
                            ] = metadata
                            trade_rows.extend(
                                normalize_trades(
                                    event_ticker=game.event_ticker,
                                    source=trade_source,
                                    payloads=trades,
                                )
                            )
                        except Exception as exc:
                            self._record_resource_error(
                                errors,
                                f"{trade_source}_trades",
                                game,
                                ticker,
                                exc,
                            )

            trade_rows = list(
                {
                    str(row["trade_id"]): row
                    for row in sorted(
                        trade_rows,
                        key=lambda item: (
                            item["created_time_utc"],
                            item["trade_id"],
                        ),
                    )
                }.values()
            )

            play_rows: list[dict[str, Any]] = []
            for game_pk in sorted({match.mlb.game_pk for match in matches}):
                try:
                    payload = mlb.get_play_by_play(game_pk)
                    play_rows.extend(normalize_plays(game_pk, payload))
                except Exception as exc:
                    errors.append(
                        {
                            "resource": "mlb_play_by_play",
                            "game_pk": game_pk,
                            "error_type": type(exc).__name__,
                            "error": str(exc),
                        }
                    )
                    self._log.error(
                        "resource_fetch_failed",
                        resource="mlb_play_by_play",
                        game_pk=game_pk,
                        error_type=type(exc).__name__,
                        error=str(exc),
                    )

        paths = {
            "markets": normalized.write("kalshi_markets", market_rows, MARKET_SCHEMA),
            "candlesticks": normalized.write(
                "kalshi_candlesticks_1m", candle_rows, CANDLE_SCHEMA
            ),
            "trades": normalized.write("kalshi_trades", trade_rows, TRADE_SCHEMA),
            "mlb_games": normalized.write(
                "mlb_schedule", [_mlb_game_row(game) for game in mlb_games], MLB_GAME_SCHEMA
            ),
            "mlb_plays": normalized.write("mlb_plays", play_rows, PLAY_SCHEMA),
            "matches": normalized.write(
                "game_matches", [_match_row(match) for match in matches], MATCH_SCHEMA
            ),
            "rejections": normalized.write(
                "game_rejections",
                [_rejection_row(rejection) for rejection in rejections],
                REJECTION_SCHEMA,
            ),
        }
        rejection_json_path = layout.run_dir / "rejections.json"
        write_manifest(
            rejection_json_path,
            {
                "rejections": [
                    {
                        "event_ticker": rejection.event_ticker,
                        "reason_code": rejection.reason_code,
                        "reason": rejection.reason,
                        "market_start_utc": rejection.market_start_utc,
                        "market_teams": rejection.market_teams,
                        "details": rejection.details,
                    }
                    for rejection in rejections
                ]
            },
        )

        counts = {
            "kalshi_games_selected": len(market_games),
            "kalshi_markets": len(market_rows),
            "candlesticks_1m": len(candle_rows),
            "trades": len(trade_rows),
            "mlb_schedule_games": len(mlb_games),
            "matched_games": len(matches),
            "rejected_games": len(rejections),
            "plays": len(play_rows),
            "resource_errors": len(errors),
        }
        manifest = {
            "run_id": layout.run_id,
            "run_type": "smoke",
            "created_at_utc": utc_iso(datetime.now(UTC)),
            "configuration": {
                "max_games": self.settings.max_games,
                "max_discovery_pages": self.settings.max_discovery_pages,
                "max_trade_pages": self.settings.max_trade_pages,
                "match_tolerance_minutes": self.settings.match_tolerance_minutes,
            },
            "cutoff": cutoff,
            "counts": counts,
            "rejection_reason_counts": _counts(
                rejection.reason_code for rejection in rejections
            ),
            "resource_errors": errors,
            "pagination": pagination,
            "raw_dir": str(layout.raw_dir.resolve()),
            "normalized_files": {
                key: str(path.resolve()) for key, path in paths.items()
            },
            "rejections_json": str(rejection_json_path.resolve()),
        }
        manifest_path = layout.run_dir / "manifest.json"
        write_manifest(manifest_path, manifest)
        self._log.info("smoke_complete", **counts)
        return {**manifest, "manifest": str(manifest_path.resolve())}

    def _clients(self, raw: RawStore) -> _ClientContext:
        return _ClientContext(self.settings, raw)

    def _probe(
        self, checks: list[dict[str, Any]], name: str, call: Callable[[], T]
    ) -> T | None:
        started = datetime.now(UTC)
        try:
            result = call()
            elapsed_ms = round((datetime.now(UTC) - started).total_seconds() * 1000, 1)
            checks.append(
                {"name": name, "status": "available", "elapsed_ms": elapsed_ms}
            )
            return result
        except Exception as exc:
            elapsed_ms = round((datetime.now(UTC) - started).total_seconds() * 1000, 1)
            checks.append(
                {
                    "name": name,
                    "status": "failed",
                    "elapsed_ms": elapsed_ms,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }
            )
            self._log.error(
                "probe_failed",
                endpoint=name,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            return None

    def _record_resource_error(
        self,
        errors: list[dict[str, Any]],
        resource: str,
        game: KalshiGame,
        ticker: str,
        exc: Exception,
    ) -> None:
        error = {
            "resource": resource,
            "event_ticker": game.event_ticker,
            "ticker": ticker,
            "error_type": type(exc).__name__,
            "error": str(exc),
        }
        errors.append(error)
        self._log.error("resource_fetch_failed", **error)


class _ClientContext:
    def __init__(self, settings: Settings, raw: RawStore) -> None:
        self._settings = settings
        self._raw = raw
        self._kalshi_http: ResilientJsonClient | None = None
        self._mlb_http: ResilientJsonClient | None = None

    def __enter__(self) -> tuple[KalshiClient, MlbClient]:
        self._kalshi_http = ResilientJsonClient(
            base_url=self._settings.kalshi_base_url,
            timeout_seconds=self._settings.http_timeout_seconds,
            max_attempts=self._settings.max_attempts,
            backoff_base_seconds=self._settings.backoff_base_seconds,
            backoff_cap_seconds=self._settings.backoff_cap_seconds,
            user_agent=self._settings.user_agent,
        )
        self._mlb_http = ResilientJsonClient(
            base_url=self._settings.mlb_base_url,
            timeout_seconds=self._settings.http_timeout_seconds,
            max_attempts=self._settings.max_attempts,
            backoff_base_seconds=self._settings.backoff_base_seconds,
            backoff_cap_seconds=self._settings.backoff_cap_seconds,
            user_agent=self._settings.user_agent,
        )
        return (
            KalshiClient(
                self._kalshi_http, self._raw, page_size=self._settings.page_size
            ),
            MlbClient(self._mlb_http, self._raw),
        )

    def __exit__(self, *_: object) -> None:
        if self._kalshi_http is not None:
            self._kalshi_http.close()
        if self._mlb_http is not None:
            self._mlb_http.close()


def _first_market(result: object) -> dict[str, Any] | None:
    if not isinstance(result, tuple) or not result:
        return None
    markets = result[0]
    if not isinstance(markets, list) or not markets:
        return None
    market = markets[0]
    return market if isinstance(market, dict) else None


def _sample_game_date(market: dict[str, Any] | None) -> date:
    if market:
        occurrence = optional_utc(market.get("occurrence_datetime"))
        ticker = str(market.get("event_ticker", ""))
        parsed = event_game_date(ticker, occurrence)
        if parsed:
            return parsed
    return datetime.now(UTC).date()


def _market_row(game: KalshiGame, market: dict[str, Any]) -> dict[str, Any]:
    return {
        "event_ticker": game.event_ticker,
        "ticker": str(market.get("ticker", "")),
        "source": game.source,
        "status": _string(market.get("status")),
        "result": _string(market.get("result")),
        "title": _string(market.get("title")),
        "yes_team": _string(market.get("yes_sub_title")),
        "no_team": _string(market.get("no_sub_title")),
        "open_time_utc": optional_utc(market.get("open_time")),
        "close_time_utc": optional_utc(market.get("close_time")),
        "occurrence_time_utc": optional_utc(market.get("occurrence_datetime")),
        "settlement_time_utc": optional_utc(market.get("settlement_ts")),
        "volume_fp": _string(market.get("volume_fp")),
        "open_interest_fp": _string(market.get("open_interest_fp")),
    }


def _mlb_game_row(game: MlbGame) -> dict[str, Any]:
    return {
        "game_pk": game.game_pk,
        "official_date": game.official_date,
        "scheduled_start_utc": game.scheduled_start_utc,
        "away_team": game.away_team,
        "home_team": game.home_team,
        "away_name": game.away_name,
        "home_name": game.home_name,
        "away_score": game.away_score,
        "home_score": game.home_score,
        "winner": mlb_winner(game),
        "final": game.final,
        "detailed_state": game.detailed_state,
        "doubleheader": game.doubleheader,
        "game_number": game.game_number,
    }


def _match_row(match: MatchedGame) -> dict[str, Any]:
    return {
        "event_ticker": match.kalshi.event_ticker,
        "game_pk": match.mlb.game_pk,
        "source": match.kalshi.source,
        "market_start_utc": match.kalshi.scheduled_start_utc,
        "mlb_start_utc": match.mlb.scheduled_start_utc,
        "start_delta_seconds": match.start_delta_seconds,
        "away_team": match.mlb.away_team,
        "home_team": match.mlb.home_team,
        "winner": match.kalshi.winner,
    }


def _rejection_row(rejection: Rejection) -> dict[str, Any]:
    return {
        "event_ticker": rejection.event_ticker,
        "reason_code": rejection.reason_code,
        "reason": rejection.reason,
        "market_start_utc": rejection.market_start_utc,
        "market_teams_json": json.dumps(rejection.market_teams),
        "details_json": json.dumps(rejection.details, sort_keys=True),
    }


def _counts(values: Iterable[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return counts


def _string(value: object) -> str | None:
    return None if value is None else str(value)
