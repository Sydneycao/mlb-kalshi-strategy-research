from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Literal

MarketSource = Literal["current", "historical"]


@dataclass(frozen=True, slots=True)
class KalshiGame:
    event_ticker: str
    source: MarketSource
    markets: tuple[dict[str, Any], ...]
    teams: tuple[str, ...]
    scheduled_start_utc: datetime | None
    game_date: date | None
    winner: str | None
    construction_errors: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class MlbGame:
    game_pk: int
    official_date: date
    scheduled_start_utc: datetime
    away_team: str
    home_team: str
    away_name: str
    home_name: str
    away_score: int | None
    home_score: int | None
    final: bool
    detailed_state: str
    doubleheader: bool
    game_number: int | None
    raw: dict[str, Any]


@dataclass(frozen=True, slots=True)
class MatchedGame:
    kalshi: KalshiGame
    mlb: MlbGame
    start_delta_seconds: int | None


@dataclass(frozen=True, slots=True)
class Rejection:
    event_ticker: str
    reason_code: str
    reason: str
    market_start_utc: datetime | None
    market_teams: tuple[str, ...]
    details: dict[str, Any]
