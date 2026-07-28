from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from mlb_kalshi.models import KalshiGame, MarketSource, MlbGame
from mlb_kalshi.teams import normalize_team
from mlb_kalshi.time import optional_utc, parse_utc

_EVENT_DATE = re.compile(
    r"^KXMLBGAME-(?P<year>\d{2})(?P<month>[A-Z]{3})(?P<day>\d{2})"
    r"(?:(?P<hour>\d{2})(?P<minute>\d{2}))?"
)
_MONTHS = {
    month: index
    for index, month in enumerate(
        ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"),
        start=1,
    )
}
_EASTERN = ZoneInfo("America/New_York")


def event_game_date(event_ticker: str, scheduled_start: datetime | None) -> date | None:
    match = _EVENT_DATE.match(event_ticker)
    if match:
        year = 2000 + int(match.group("year"))
        return date(year, _MONTHS[match.group("month")], int(match.group("day")))
    if scheduled_start is not None:
        return parse_utc(scheduled_start).astimezone(_EASTERN).date()
    return None


def event_scheduled_start(
    event_ticker: str, occurrence_time: datetime | None
) -> datetime | None:
    """Decode KXMLBGAME's US Eastern scheduled start and return it in UTC."""

    match = _EVENT_DATE.match(event_ticker)
    if match and match.group("hour") and match.group("minute"):
        eastern = datetime(
            2000 + int(match.group("year")),
            _MONTHS[match.group("month")],
            int(match.group("day")),
            int(match.group("hour")),
            int(match.group("minute")),
            tzinfo=_EASTERN,
        )
        return eastern.astimezone(UTC)
    return parse_utc(occurrence_time) if occurrence_time is not None else None


def _market_team_names(market: dict[str, Any]) -> tuple[str, ...]:
    # KXMLBGAME's no_sub_title repeats the contract's yes team; the opposing
    # team is represented by the complementary contract in the same event.
    names = (
        [str(market["yes_sub_title"]).strip()]
        if market.get("yes_sub_title")
        else []
    )
    if not names:
        title = str(market.get("title", ""))
        match = re.match(r"(.+?)\s+vs\.?\s+(.+?)(?:\s+Winner\?)?$", title, re.I)
        if match:
            names.extend((match.group(1), match.group(2)))
    return tuple(names)


def build_kalshi_game(
    event_ticker: str,
    source: MarketSource,
    markets: Iterable[dict[str, Any]],
) -> KalshiGame:
    market_tuple = tuple(markets)
    errors: list[str] = []
    if not market_tuple:
        errors.append("event has no markets")

    starts = {
        timestamp
        for market in market_tuple
        if (timestamp := optional_utc(market.get("occurrence_datetime"))) is not None
    }
    occurrence_time = min(starts) if starts else None
    scheduled_start = event_scheduled_start(event_ticker, occurrence_time)
    if len(starts) > 1:
        errors.append("markets disagree on occurrence_datetime")

    team_codes: set[str] = set()
    inferred_winners: set[str] = set()
    for market in market_tuple:
        raw_names = _market_team_names(market)
        team_codes.update(normalize_team(name) for name in raw_names)

        result = str(market.get("result", "")).lower()
        yes_name = market.get("yes_sub_title")
        if result == "yes" and yes_name:
            inferred_winners.add(normalize_team(str(yes_name)))

    teams = tuple(sorted(team_codes))
    if len(teams) != 2:
        errors.append(f"expected exactly two teams, found {len(teams)}")
    if any(team.startswith("UNKNOWN:") for team in teams):
        errors.append("one or more Kalshi team names are unknown")

    if len(inferred_winners) == 1:
        winner = next(iter(inferred_winners))
    else:
        winner = None
        if not inferred_winners:
            errors.append("no settled winner could be inferred")
        else:
            errors.append("market results imply conflicting winners")

    return KalshiGame(
        event_ticker=event_ticker,
        source=source,
        markets=market_tuple,
        teams=teams,
        scheduled_start_utc=scheduled_start,
        game_date=event_game_date(event_ticker, scheduled_start),
        winner=winner,
        construction_errors=tuple(errors),
    )


def normalize_mlb_schedule(payload: dict[str, Any]) -> list[MlbGame]:
    games: list[MlbGame] = []
    for date_entry in payload.get("dates", []):
        for raw_game in date_entry.get("games", []):
            away = raw_game.get("teams", {}).get("away", {})
            home = raw_game.get("teams", {}).get("home", {})
            away_name = str(away.get("team", {}).get("name", ""))
            home_name = str(home.get("team", {}).get("name", ""))
            status = raw_game.get("status", {})
            game_date_value = raw_game.get("gameDate")
            official_date_value = raw_game.get("officialDate")
            game_pk = raw_game.get("gamePk")
            if not (
                isinstance(game_pk, int)
                and isinstance(game_date_value, str)
                and isinstance(official_date_value, str)
                and away_name
                and home_name
            ):
                continue
            games.append(
                MlbGame(
                    game_pk=game_pk,
                    official_date=date.fromisoformat(official_date_value),
                    scheduled_start_utc=parse_utc(game_date_value),
                    away_team=normalize_team(away_name),
                    home_team=normalize_team(home_name),
                    away_name=away_name,
                    home_name=home_name,
                    away_score=_optional_int(away.get("score")),
                    home_score=_optional_int(home.get("score")),
                    final=str(status.get("abstractGameState", "")).lower() == "final",
                    detailed_state=str(status.get("detailedState", "")),
                    doubleheader=str(raw_game.get("doubleHeader", "N")).upper() != "N",
                    game_number=_optional_int(raw_game.get("gameNumber")),
                    raw=raw_game,
                )
            )
    return games


def normalize_candlesticks(
    *,
    event_ticker: str,
    market_ticker: str,
    source: MarketSource,
    payload: dict[str, Any],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for candle in payload.get("candlesticks", []):
        end_ts = candle.get("end_period_ts")
        if not isinstance(end_ts, (int, float)):
            continue
        row: dict[str, Any] = {
            "event_ticker": event_ticker,
            "ticker": market_ticker,
            "source": source,
            "end_period_time_utc": datetime.fromtimestamp(end_ts, tz=UTC),
            "volume_fp": _as_string(
                candle.get("volume_fp", candle.get("volume"))
            ),
            "open_interest_fp": _as_string(
                candle.get("open_interest_fp", candle.get("open_interest"))
            ),
        }
        for quote in ("yes_bid", "yes_ask", "price"):
            values = candle.get(quote) or {}
            for field in (
                "open_dollars",
                "low_dollars",
                "high_dollars",
                "close_dollars",
                "mean_dollars",
                "previous_dollars",
                "min_dollars",
                "max_dollars",
            ):
                legacy_field = field.removesuffix("_dollars")
                row[f"{quote}_{field}"] = _as_string(
                    values.get(field, values.get(legacy_field))
                )
        rows.append(row)
    return rows


def normalize_trades(
    *,
    event_ticker: str,
    source: str,
    payloads: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for trade in payloads:
        created_time = optional_utc(trade.get("created_time"))
        trade_id = str(trade.get("trade_id", ""))
        ticker = str(trade.get("ticker", ""))
        if not trade_id or not ticker or created_time is None:
            continue
        rows[trade_id] = {
            "trade_id": trade_id,
            "event_ticker": event_ticker,
            "ticker": ticker,
            "source": source,
            "created_time_utc": created_time,
            "count_fp": _as_string(trade.get("count_fp")),
            "yes_price_dollars": _as_string(trade.get("yes_price_dollars")),
            "no_price_dollars": _as_string(trade.get("no_price_dollars")),
            "taker_side": _as_string(trade.get("taker_side")),
        }
    return sorted(rows.values(), key=lambda row: (row["created_time_utc"], row["trade_id"]))


def normalize_plays(game_pk: int, payload: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    all_plays = payload.get("liveData", {}).get("plays", {}).get("allPlays", [])
    for play in all_plays:
        about = play.get("about", {})
        result = play.get("result", {})
        rows.append(
            {
                "game_pk": game_pk,
                "at_bat_index": _optional_int(about.get("atBatIndex")),
                "event": _as_string(result.get("event")),
                "description": _as_string(result.get("description")),
                "inning": _optional_int(about.get("inning")),
                "half_inning": _as_string(about.get("halfInning")),
                "start_time_utc": optional_utc(about.get("startTime")),
                "end_time_utc": optional_utc(about.get("endTime")),
                "away_score": _optional_int(result.get("awayScore")),
                "home_score": _optional_int(result.get("homeScore")),
                "is_complete": bool(about.get("isComplete", False)),
            }
        )
    return rows


def _optional_int(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if not isinstance(value, (int, float, str)):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_string(value: object) -> str | None:
    return None if value is None else str(value)
