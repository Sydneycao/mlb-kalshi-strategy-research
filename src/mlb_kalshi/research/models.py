from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any


@dataclass(frozen=True, slots=True)
class PlayObservation:
    game_pk: int
    observed_at_utc: datetime
    at_bat_index: int
    event: str
    description: str
    inning: int
    half_inning: str
    outs: int
    away_score: int
    home_score: int
    away_score_delta: int
    home_score_delta: int
    batting_team: str
    on_first: bool
    on_second: bool
    on_third: bool
    half_inning_ended: bool
    game_over_observed: bool


@dataclass(frozen=True, slots=True)
class StatusObservation:
    observed_at_utc: datetime
    phase: str
    description: str


@dataclass(frozen=True, slots=True)
class TradePlan:
    plan_id: str
    strategy: str
    event_ticker: str
    ticker: str
    entry_signal_minute_start_utc: datetime
    entry_signal_after_utc: datetime
    exit_signal_minute_start_utc: datetime
    exit_signal_after_utc: datetime
    entry_reason: str
    exit_reason: str
    parameters: dict[str, Any]


@dataclass(frozen=True, slots=True)
class QuoteFill:
    execution_at_utc: datetime
    price: Decimal
    bid: Decimal | None
    ask: Decimal | None
    spread: Decimal | None
    delay_seconds: int
    missing_quote_minutes: int
