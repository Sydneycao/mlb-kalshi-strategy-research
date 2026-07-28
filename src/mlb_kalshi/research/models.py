from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

CONTRACT_QUANTUM = Decimal("0.01")


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
class ExecutionConfig:
    contracts_per_trade: Decimal = Decimal("1.00")
    max_volume_participation: Decimal = Decimal("0.1000")
    taker_fee_rate: Decimal = Decimal("0.0700")
    fee_multiplier: Decimal = Decimal("1.0000")
    fee_rounding_quantum: Decimal = Decimal("0.0100")

    def validate(self) -> None:
        if self.contracts_per_trade <= 0:
            raise ValueError("contracts_per_trade must be positive")
        if (
            self.contracts_per_trade.quantize(CONTRACT_QUANTUM)
            != self.contracts_per_trade
        ):
            raise ValueError("contracts_per_trade must use 0.01-contract increments")
        if not Decimal(0) < self.max_volume_participation <= Decimal(1):
            raise ValueError("max_volume_participation must be greater than 0 and at most 1")
        if self.taker_fee_rate < 0:
            raise ValueError("taker_fee_rate cannot be negative")
        if self.fee_multiplier < 0:
            raise ValueError("fee_multiplier cannot be negative")
        if self.fee_rounding_quantum <= 0:
            raise ValueError("fee_rounding_quantum must be positive")


@dataclass(frozen=True, slots=True)
class QuoteFill:
    execution_at_utc: datetime
    price: Decimal
    bid: Decimal | None
    ask: Decimal | None
    spread: Decimal | None
    delay_seconds: int
    missing_quote_minutes: int
    compatible_trade_volume: Decimal
    capacity_contracts: Decimal
    insufficient_capacity_minutes: int
