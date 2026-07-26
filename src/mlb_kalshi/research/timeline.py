from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from mlb_kalshi.research.models import PlayObservation, StatusObservation
from mlb_kalshi.teams import normalize_team
from mlb_kalshi.time import optional_utc, parse_utc

MINUTE = timedelta(minutes=1)
PRICE_QUANTUM = Decimal("0.0001")


def floor_minute(value: datetime) -> datetime:
    return parse_utc(value).replace(second=0, microsecond=0)


def ceil_minute(value: datetime) -> datetime:
    floored = floor_minute(value)
    return floored if value == floored else floored + MINUTE


def parse_game_observations(
    *,
    game_pk: int,
    away_team: str,
    home_team: str,
    payload: dict[str, Any],
) -> tuple[list[PlayObservation], list[StatusObservation]]:
    """Convert raw MLB play-by-play to states observable at each play's end."""

    raw_plays = payload.get("liveData", {}).get("plays", {}).get("allPlays", [])
    plays = sorted(
        (play for play in raw_plays if isinstance(play, dict)),
        key=lambda play: int(play.get("about", {}).get("atBatIndex", 0)),
    )
    observations: list[PlayObservation] = []
    statuses: list[StatusObservation] = []
    occupied: dict[str, str] = {}
    previous_half: tuple[int, str] | None = None
    previous_away_score = 0
    previous_home_score = 0

    for play in plays:
        about = play.get("about", {})
        result = play.get("result", {})
        count = play.get("count", {})
        observed_at = optional_utc(about.get("endTime"))
        if observed_at is None:
            continue

        inning = _int(about.get("inning"), 0)
        half = str(about.get("halfInning", "")).lower()
        half_key = (inning, half)
        if previous_half is not None and half_key != previous_half:
            occupied.clear()
        previous_half = half_key

        for movement in sorted(
            (item for item in play.get("runners", []) if isinstance(item, dict)),
            key=lambda item: _int(item.get("details", {}).get("playIndex"), 0),
        ):
            _apply_runner_movement(occupied, movement)

        outs = _int(count.get("outs"), 0)
        half_ended = outs >= 3
        if half_ended:
            occupied.clear()

        away_score = _int(result.get("awayScore"), previous_away_score)
        home_score = _int(result.get("homeScore"), previous_home_score)
        batting_team = away_team if half == "top" else home_team
        game_over = _game_over_observed(
            inning=inning,
            half_inning=half,
            outs=outs,
            away_score=away_score,
            home_score=home_score,
        )
        observations.append(
            PlayObservation(
                game_pk=game_pk,
                observed_at_utc=observed_at,
                at_bat_index=_int(about.get("atBatIndex"), len(observations)),
                event=str(result.get("event", "")),
                description=str(result.get("description", "")),
                inning=inning,
                half_inning=half,
                outs=outs,
                away_score=away_score,
                home_score=home_score,
                away_score_delta=away_score - previous_away_score,
                home_score_delta=home_score - previous_home_score,
                batting_team=batting_team,
                on_first="1B" in occupied,
                on_second="2B" in occupied,
                on_third="3B" in occupied,
                half_inning_ended=half_ended,
                game_over_observed=game_over,
            )
        )
        previous_away_score = away_score
        previous_home_score = home_score

        for event in play.get("playEvents", []):
            if not isinstance(event, dict):
                continue
            details = event.get("details", {})
            description = str(details.get("description", ""))
            if str(details.get("eventType", "")).lower() != "game_advisory":
                continue
            phase = _phase_from_advisory(description)
            event_time = optional_utc(event.get("endTime"))
            if phase is not None and event_time is not None:
                statuses.append(
                    StatusObservation(
                        observed_at_utc=event_time,
                        phase=phase,
                        description=description,
                    )
                )

    observations.sort(key=lambda item: (item.observed_at_utc, item.at_bat_index))
    statuses.sort(key=lambda item: item.observed_at_utc)
    return observations, statuses


def build_minute_timeline(
    *,
    matches: list[dict[str, Any]],
    markets: list[dict[str, Any]],
    candles: list[dict[str, Any]],
    raw_dir: Path,
    pregame_minutes: int,
) -> list[dict[str, Any]]:
    """Build a continuous per-market grid without forward-filling quotes."""

    matches_by_event = {str(row["event_ticker"]): row for row in matches}
    markets_by_event: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for market in markets:
        markets_by_event[str(market["event_ticker"])].append(market)

    candle_by_ticker_minute: dict[tuple[str, datetime], dict[str, Any]] = {}
    for candle in candles:
        end = optional_utc(candle.get("end_period_time_utc"))
        ticker = str(candle.get("ticker", ""))
        if end is not None and ticker:
            candle_by_ticker_minute[(ticker, end - MINUTE)] = candle

    timeline: list[dict[str, Any]] = []
    for event_ticker, match in matches_by_event.items():
        game_pk = int(match["game_pk"])
        scheduled_start = parse_utc(match["mlb_start_utc"])
        away_team = str(match["away_team"])
        home_team = str(match["home_team"])
        payload_path = raw_dir / "mlb" / "play_by_play" / f"game_{game_pk}.json"
        if not payload_path.exists():
            raise FileNotFoundError(f"missing raw MLB play-by-play: {payload_path}")
        payload = json.loads(payload_path.read_text(encoding="utf-8"))
        observations, statuses = parse_game_observations(
            game_pk=game_pk,
            away_team=away_team,
            home_team=home_team,
            payload=payload,
        )
        if not observations:
            raise ValueError(f"game {game_pk} has no timestamped play observations")

        event_markets = markets_by_event[event_ticker]
        for market in event_markets:
            timeline.extend(
                _build_market_minutes(
                    event_ticker=event_ticker,
                    match=match,
                    market=market,
                    scheduled_start=scheduled_start,
                    observations=observations,
                    statuses=statuses,
                    candle_by_ticker_minute=candle_by_ticker_minute,
                    pregame_minutes=pregame_minutes,
                )
            )

    timeline.sort(
        key=lambda row: (
            row["event_ticker"],
            row["ticker"],
            row["minute_start_utc"],
        )
    )
    return timeline


def _build_market_minutes(
    *,
    event_ticker: str,
    match: dict[str, Any],
    market: dict[str, Any],
    scheduled_start: datetime,
    observations: list[PlayObservation],
    statuses: list[StatusObservation],
    candle_by_ticker_minute: dict[tuple[str, datetime], dict[str, Any]],
    pregame_minutes: int,
) -> list[dict[str, Any]]:
    ticker = str(market["ticker"])
    yes_team = normalize_team(str(market["yes_team"]))
    away_team = str(match["away_team"])
    home_team = str(match["home_team"])
    opponent_team = home_team if yes_team == away_team else away_team
    open_time = parse_utc(market["open_time_utc"])
    settlement_time = parse_utc(market["settlement_time_utc"])
    start = max(
        floor_minute(open_time),
        floor_minute(scheduled_start) - timedelta(minutes=pregame_minutes),
    )
    end = ceil_minute(settlement_time)

    rows: list[dict[str, Any]] = []
    play_index = 0
    status_index = 0
    latest_play: PlayObservation | None = None
    phase = "pregame"
    minute_start = start
    while minute_start < end:
        minute_end = minute_start + MINUTE
        minute_plays: list[PlayObservation] = []
        minute_statuses: list[StatusObservation] = []

        while (
            status_index < len(statuses)
            and statuses[status_index].observed_at_utc < minute_end
        ):
            status = statuses[status_index]
            if status.observed_at_utc >= minute_start:
                minute_statuses.append(status)
            phase = status.phase
            status_index += 1

        while (
            play_index < len(observations)
            and observations[play_index].observed_at_utc < minute_end
        ):
            play = observations[play_index]
            if play.observed_at_utc >= minute_start:
                minute_plays.append(play)
            latest_play = play
            if phase in {"pregame", "delayed"}:
                phase = "live"
            if play.game_over_observed:
                phase = "postgame"
            play_index += 1

        candle = candle_by_ticker_minute.get((ticker, minute_start))
        bid_open = _decimal(candle.get("yes_bid_open_dollars")) if candle else None
        ask_open = _decimal(candle.get("yes_ask_open_dollars")) if candle else None
        bid_close = _decimal(candle.get("yes_bid_close_dollars")) if candle else None
        ask_close = _decimal(candle.get("yes_ask_close_dollars")) if candle else None
        price_close = _decimal(candle.get("price_close_dollars")) if candle else None
        open_spread = _spread(bid_open, ask_open)
        close_spread = _spread(bid_close, ask_close)

        away_score = latest_play.away_score if latest_play else 0
        home_score = latest_play.home_score if latest_play else 0
        yes_score = away_score if yes_team == away_team else home_score
        opponent_score = home_score if yes_team == away_team else away_score
        batting_team = latest_play.batting_team if latest_play else None
        on_first = latest_play.on_first if latest_play else False
        on_second = latest_play.on_second if latest_play else False
        on_third = latest_play.on_third if latest_play else False
        base_runners = sum((on_first, on_second, on_third))
        risp = sum((on_second, on_third))
        away_scored = sum(play.away_score_delta for play in minute_plays)
        home_scored = sum(play.home_score_delta for play in minute_plays)
        yes_scored = away_scored if yes_team == away_team else home_scored
        opponent_scored = home_scored if yes_team == away_team else away_scored

        rows.append(
            {
                "event_ticker": event_ticker,
                "game_pk": int(match["game_pk"]),
                "ticker": ticker,
                "source": str(market["source"]),
                "yes_team": yes_team,
                "opponent_team": opponent_team,
                "away_team": away_team,
                "home_team": home_team,
                "scheduled_start_utc": scheduled_start,
                "minute_start_utc": minute_start,
                "minute_end_utc": minute_end,
                "minutes_from_scheduled_start": int(
                    (minute_start - scheduled_start).total_seconds() // 60
                ),
                "game_phase": phase,
                "quote_observed": candle is not None,
                "yes_bid_open": bid_open,
                "yes_ask_open": ask_open,
                "yes_bid_close": bid_close,
                "yes_ask_close": ask_close,
                "yes_price_close": price_close,
                "yes_open_spread": open_spread,
                "yes_close_spread": close_spread,
                "missing_yes_bid_open": bid_open is None,
                "missing_yes_ask_open": ask_open is None,
                "volume_fp": str(candle.get("volume_fp")) if candle else None,
                "open_interest_fp": (
                    str(candle.get("open_interest_fp")) if candle else None
                ),
                "observed_play_count": len(minute_plays),
                "observed_scoring_play_count": sum(
                    play.away_score_delta > 0 or play.home_score_delta > 0
                    for play in minute_plays
                ),
                "observed_events_json": json.dumps(
                    [
                        {
                            "observed_at_utc": play.observed_at_utc.isoformat(),
                            "event": play.event,
                            "description": play.description,
                        }
                        for play in minute_plays
                    ],
                    separators=(",", ":"),
                ),
                "observed_statuses_json": json.dumps(
                    [
                        {
                            "observed_at_utc": status.observed_at_utc.isoformat(),
                            "phase": status.phase,
                            "description": status.description,
                        }
                        for status in minute_statuses
                    ],
                    separators=(",", ":"),
                ),
                "last_event_observed_at_utc": (
                    latest_play.observed_at_utc if latest_play else None
                ),
                "last_event": latest_play.event if latest_play else None,
                "last_event_description": (
                    latest_play.description if latest_play else None
                ),
                "inning": latest_play.inning if latest_play else None,
                "half_inning": latest_play.half_inning if latest_play else None,
                "outs": latest_play.outs if latest_play else None,
                "batting_team": batting_team,
                "yes_team_is_batting": (
                    batting_team == yes_team if batting_team is not None else None
                ),
                "away_score": away_score,
                "home_score": home_score,
                "yes_score": yes_score,
                "opponent_score": opponent_score,
                "yes_lead": yes_score - opponent_score,
                "yes_runs_scored_this_minute": yes_scored,
                "opponent_runs_scored_this_minute": opponent_scored,
                "score_changed_this_minute": away_scored > 0 or home_scored > 0,
                "on_first": on_first,
                "on_second": on_second,
                "on_third": on_third,
                "base_runners": base_runners,
                "runners_in_scoring_position": risp,
                "threat_against_yes": (
                    phase == "live"
                    and batting_team == opponent_team
                    and risp > 0
                    and (latest_play.outs if latest_play else 3) < 3
                ),
                "half_inning_ended_this_minute": any(
                    play.half_inning_ended for play in minute_plays
                ),
                "game_over_observed": (
                    latest_play.game_over_observed if latest_play else False
                ),
            }
        )
        minute_start = minute_end
    return rows


def _apply_runner_movement(
    occupied: dict[str, str], movement: dict[str, Any]
) -> None:
    details = movement.get("details", {})
    move = movement.get("movement", {})
    runner = details.get("runner", {})
    runner_key = str(runner.get("id") or runner.get("fullName") or "")
    if not runner_key:
        return
    for base, existing_runner in list(occupied.items()):
        if existing_runner == runner_key:
            occupied.pop(base, None)
    start = move.get("start") or move.get("originBase")
    if isinstance(start, str):
        occupied.pop(start, None)
    end = move.get("end")
    if not bool(move.get("isOut")) and end in {"1B", "2B", "3B"}:
        occupied[str(end)] = runner_key


def _game_over_observed(
    *,
    inning: int,
    half_inning: str,
    outs: int,
    away_score: int,
    home_score: int,
) -> bool:
    if inning < 9:
        return False
    if half_inning == "top":
        return outs >= 3 and home_score > away_score
    if home_score > away_score:
        return True
    return outs >= 3 and away_score != home_score


def _phase_from_advisory(description: str) -> str | None:
    normalized = description.lower()
    if "in progress" in normalized:
        return "live"
    if "final" in normalized or "game over" in normalized:
        return "postgame"
    if "delayed" in normalized or "suspended" in normalized:
        return "delayed"
    if "pre-game" in normalized or "pregame" in normalized or "warmup" in normalized:
        return "pregame"
    return None


def _spread(bid: Decimal | None, ask: Decimal | None) -> Decimal | None:
    if bid is None or ask is None:
        return None
    return (ask - bid).quantize(PRICE_QUANTUM)


def _decimal(value: object) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value)).quantize(PRICE_QUANTUM)
    except (InvalidOperation, ValueError):
        return None


def _int(value: object, default: int) -> int:
    if isinstance(value, bool) or value is None:
        return default
    if not isinstance(value, (int, float, str)):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
