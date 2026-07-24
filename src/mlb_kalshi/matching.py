from __future__ import annotations

from datetime import timedelta
from typing import Any

from mlb_kalshi.models import KalshiGame, MatchedGame, MlbGame, Rejection


def mlb_winner(game: MlbGame) -> str | None:
    if not game.final or game.away_score is None or game.home_score is None:
        return None
    if game.away_score == game.home_score:
        return None
    return game.away_team if game.away_score > game.home_score else game.home_team


def result_matches(kalshi_winner: str | None, game: MlbGame) -> bool:
    return kalshi_winner is not None and kalshi_winner == mlb_winner(game)


def match_game(
    market_game: KalshiGame,
    mlb_games: list[MlbGame],
    *,
    tolerance: timedelta,
) -> MatchedGame | Rejection:
    base_details: dict[str, Any] = {
        "construction_errors": list(market_game.construction_errors),
        "game_date": market_game.game_date.isoformat() if market_game.game_date else None,
        "winner": market_game.winner,
        "schedule_candidates": len(mlb_games),
    }
    if market_game.construction_errors:
        return _rejection(
            market_game,
            "MALFORMED_MARKET_GROUP",
            "; ".join(market_game.construction_errors),
            base_details,
        )
    if (
        len(market_game.teams) != 2
        or market_game.scheduled_start_utc is None
        or market_game.game_date is None
        or market_game.winner is None
    ):
        return _rejection(
            market_game,
            "INCOMPLETE_MARKET_IDENTITY",
            "market event lacks teams, date, scheduled start, or final result",
            base_details,
        )

    market_pair = frozenset(market_game.teams)
    team_matches = [
        game
        for game in mlb_games
        if frozenset((game.away_team, game.home_team)) == market_pair
    ]
    if not team_matches:
        base_details["observed_team_pairs"] = sorted(
            {f"{game.away_team}@{game.home_team}" for game in mlb_games}
        )
        return _rejection(
            market_game,
            "NO_TEAM_PAIR_MATCH",
            "no MLB schedule entry has the same two normalized teams",
            base_details,
        )

    date_matches = [game for game in team_matches if game.official_date == market_game.game_date]
    if not date_matches:
        base_details["team_match_dates"] = sorted(
            {game.official_date.isoformat() for game in team_matches}
        )
        return _rejection(
            market_game,
            "DATE_MISMATCH",
            "team pair exists in the schedule, but not on the Kalshi event date",
            base_details,
        )

    with_deltas = sorted(
        (
            (
                abs(
                    int(
                        (
                            game.scheduled_start_utc - market_game.scheduled_start_utc
                        ).total_seconds()
                    )
                ),
                game,
            )
            for game in date_matches
        ),
        key=lambda pair: (pair[0], pair[1].game_pk),
    )
    base_details["date_match_start_deltas_seconds"] = [
        {"game_pk": game.game_pk, "delta_seconds": delta} for delta, game in with_deltas
    ]
    if not with_deltas or with_deltas[0][0] > tolerance.total_seconds():
        return _rejection(
            market_game,
            "START_TIME_MISMATCH",
            (
                "nearest scheduled start exceeds the "
                f"{int(tolerance.total_seconds() / 60)}-minute tolerance"
            ),
            base_details,
        )

    nearest_delta = with_deltas[0][0]
    nearest = [game for delta, game in with_deltas if delta == nearest_delta]
    if len(nearest) != 1:
        base_details["ambiguous_game_pks"] = [game.game_pk for game in nearest]
        return _rejection(
            market_game,
            "AMBIGUOUS_DOUBLEHEADER",
            "multiple same-team games have equally close scheduled start times",
            base_details,
        )

    selected = nearest[0]
    selected_winner = mlb_winner(selected)
    if selected_winner is None:
        base_details.update(
            {
                "selected_game_pk": selected.game_pk,
                "selected_status": selected.detailed_state,
                "selected_score": {
                    "away": selected.away_score,
                    "home": selected.home_score,
                },
            }
        )
        return _rejection(
            market_game,
            "MLB_RESULT_UNAVAILABLE",
            "nearest MLB game is not final or has no decisive score",
            base_details,
        )
    if not result_matches(market_game.winner, selected):
        base_details.update(
            {
                "selected_game_pk": selected.game_pk,
                "kalshi_winner": market_game.winner,
                "mlb_winner": selected_winner,
                "selected_score": {
                    "away": selected.away_score,
                    "home": selected.home_score,
                },
            }
        )
        return _rejection(
            market_game,
            "RESULT_MISMATCH",
            "Kalshi settlement winner disagrees with the nearest MLB final result",
            base_details,
        )

    return MatchedGame(
        kalshi=market_game,
        mlb=selected,
        start_delta_seconds=nearest_delta,
    )


def _rejection(
    market_game: KalshiGame,
    code: str,
    reason: str,
    details: dict[str, Any],
) -> Rejection:
    return Rejection(
        event_ticker=market_game.event_ticker,
        reason_code=code,
        reason=reason,
        market_start_utc=market_game.scheduled_start_utc,
        market_teams=market_game.teams,
        details=details,
    )
