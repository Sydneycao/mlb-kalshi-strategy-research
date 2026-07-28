from datetime import UTC, date, datetime, timedelta

from mlb_kalshi.matching import match_game, result_matches
from mlb_kalshi.models import KalshiGame, MatchedGame, MlbGame, Rejection
from mlb_kalshi.normalize import (
    build_kalshi_game,
    event_game_date,
    event_scheduled_start,
)


def _mlb_game(
    *,
    game_pk: int,
    start_hour: int,
    away_score: int,
    home_score: int,
    game_number: int,
) -> MlbGame:
    return MlbGame(
        game_pk=game_pk,
        official_date=date(2025, 7, 12),
        scheduled_start_utc=datetime(2025, 7, 12, start_hour, 10, tzinfo=UTC),
        away_team="BOS",
        home_team="NYY",
        away_name="Boston Red Sox",
        home_name="New York Yankees",
        away_score=away_score,
        home_score=home_score,
        final=True,
        detailed_state="Final",
        doubleheader=True,
        game_number=game_number,
        raw={},
    )


def _market_game(start_hour: int, winner: str = "BOS") -> KalshiGame:
    return KalshiGame(
        event_ticker="KXMLBGAME-25JUL121310BOSNYY",
        source="historical",
        markets=(),
        teams=("BOS", "NYY"),
        scheduled_start_utc=datetime(2025, 7, 12, start_hour, 10, tzinfo=UTC),
        game_date=date(2025, 7, 12),
        winner=winner,
    )


def test_doubleheader_is_disambiguated_by_scheduled_start() -> None:
    first = _mlb_game(
        game_pk=1, start_hour=17, away_score=5, home_score=3, game_number=1
    )
    second = _mlb_game(
        game_pk=2, start_hour=23, away_score=2, home_score=4, game_number=2
    )

    result = match_game(
        _market_game(17), [second, first], tolerance=timedelta(hours=8)
    )

    assert isinstance(result, MatchedGame)
    assert result.mlb.game_pk == 1
    assert result.start_delta_seconds == 0


def test_nearest_doubleheader_game_must_also_match_result() -> None:
    first = _mlb_game(
        game_pk=1, start_hour=17, away_score=1, home_score=3, game_number=1
    )
    second = _mlb_game(
        game_pk=2, start_hour=23, away_score=5, home_score=2, game_number=2
    )

    result = match_game(
        _market_game(17, winner="BOS"),
        [first, second],
        tolerance=timedelta(hours=8),
    )

    assert isinstance(result, Rejection)
    assert result.reason_code == "RESULT_MISMATCH"
    assert result.details["selected_game_pk"] == 1


def test_result_matching_covers_home_and_away_winners() -> None:
    away_win = _mlb_game(
        game_pk=1, start_hour=17, away_score=5, home_score=3, game_number=1
    )
    home_win = _mlb_game(
        game_pk=2, start_hour=23, away_score=2, home_score=4, game_number=2
    )
    assert result_matches("BOS", away_win)
    assert result_matches("NYY", home_win)
    assert not result_matches("NYY", away_win)


def test_kalshi_ticker_date_wins_over_utc_calendar_date() -> None:
    # A US evening game can begin after midnight UTC.
    scheduled = datetime(2025, 7, 13, 0, 10, tzinfo=UTC)
    assert event_game_date("KXMLBGAME-25JUL121910BOSNYY", scheduled) == date(
        2025, 7, 12
    )


def test_kalshi_ticker_scheduled_time_is_eastern_and_normalized_to_utc() -> None:
    assert event_scheduled_start("KXMLBGAME-25JUL121910BOSNYY", None) == datetime(
        2025, 7, 12, 23, 10, tzinfo=UTC
    )


def test_legacy_ticker_without_time_uses_occurrence_time() -> None:
    occurrence = datetime(2025, 7, 12, 23, 10, tzinfo=UTC)

    assert event_game_date("KXMLBGAME-25JUL12BOSNYY", occurrence) == date(
        2025, 7, 12
    )
    assert event_scheduled_start("KXMLBGAME-25JUL12BOSNYY", occurrence) == occurrence


def test_legacy_market_without_occurrence_time_is_still_well_formed() -> None:
    event_ticker = "KXMLBGAME-25JUL12BOSNYY"
    game = build_kalshi_game(
        event_ticker,
        "historical",
        [
            {
                "event_ticker": event_ticker,
                "ticker": f"{event_ticker}-BOS",
                "yes_sub_title": "Boston",
                "result": "yes",
            },
            {
                "event_ticker": event_ticker,
                "ticker": f"{event_ticker}-NYY",
                "yes_sub_title": "New York Y",
                "result": "no",
            },
        ],
    )

    assert game.game_date == date(2025, 7, 12)
    assert game.scheduled_start_utc is None
    assert game.construction_errors == ()


def test_legacy_market_without_time_matches_unique_team_date() -> None:
    market = _market_game(17)
    market = KalshiGame(
        event_ticker="KXMLBGAME-25JUL12BOSNYY",
        source=market.source,
        markets=market.markets,
        teams=market.teams,
        scheduled_start_utc=None,
        game_date=market.game_date,
        winner=market.winner,
    )
    game = _mlb_game(
        game_pk=1,
        start_hour=17,
        away_score=5,
        home_score=3,
        game_number=1,
    )

    result = match_game(market, [game], tolerance=timedelta(hours=2))

    assert isinstance(result, MatchedGame)
    assert result.mlb.game_pk == 1
    assert result.start_delta_seconds is None


def test_legacy_doubleheader_uses_ticker_game_number() -> None:
    market = _market_game(17)
    market = KalshiGame(
        event_ticker="KXMLBGAME-25JUL12BOSNYY2",
        source=market.source,
        markets=market.markets,
        teams=market.teams,
        scheduled_start_utc=None,
        game_date=market.game_date,
        winner="NYY",
    )
    first = _mlb_game(
        game_pk=1,
        start_hour=17,
        away_score=5,
        home_score=3,
        game_number=1,
    )
    second = _mlb_game(
        game_pk=2,
        start_hour=23,
        away_score=2,
        home_score=4,
        game_number=2,
    )

    result = match_game(market, [first, second], tolerance=timedelta(hours=2))

    assert isinstance(result, MatchedGame)
    assert result.mlb.game_pk == 2
    assert result.start_delta_seconds is None


def test_excessive_market_window_is_rejected() -> None:
    market = _market_game(17)
    market = KalshiGame(
        event_ticker=market.event_ticker,
        source=market.source,
        markets=(
            {
                "ticker": "KXMLBGAME-LONG-BOS",
                "open_time": "2025-07-01T00:00:00Z",
                "settlement_ts": "2025-07-12T00:00:00Z",
            },
        ),
        teams=market.teams,
        scheduled_start_utc=market.scheduled_start_utc,
        game_date=market.game_date,
        winner=market.winner,
    )
    game = _mlb_game(
        game_pk=1,
        start_hour=17,
        away_score=5,
        home_score=3,
        game_number=1,
    )

    result = match_game(market, [game], tolerance=timedelta(hours=2))

    assert isinstance(result, Rejection)
    assert result.reason_code == "EXCESSIVE_MARKET_WINDOW"


def test_contract_no_subtitle_does_not_imply_winner() -> None:
    shared = {
        "event_ticker": "KXMLBGAME-25JUL121310BOSNYY",
        "occurrence_datetime": "2025-07-12T17:10:00Z",
    }
    game = build_kalshi_game(
        shared["event_ticker"],
        "historical",
        [
            {
                **shared,
                "ticker": f"{shared['event_ticker']}-BOS",
                "yes_sub_title": "Boston",
                "no_sub_title": "Boston",
                "result": "yes",
            },
            {
                **shared,
                "ticker": f"{shared['event_ticker']}-NYY",
                "yes_sub_title": "New York Y",
                "no_sub_title": "New York Y",
                "result": "no",
            },
        ],
    )

    assert game.teams == ("BOS", "NYY")
    assert game.winner == "BOS"
    assert game.construction_errors == ()
