from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol

from mlb_kalshi.research.models import TradePlan

ONE_MINUTE = timedelta(minutes=1)


class Strategy(Protocol):
    @property
    def name(self) -> str: ...

    def generate(self, timeline: list[dict[str, Any]]) -> list[TradePlan]: ...


@dataclass(frozen=True, slots=True)
class PregameToLive:
    name: str = "pregame_to_live"
    entry_lead_minutes: int = 5
    live_exit_minutes: int = 15

    def generate(self, timeline: list[dict[str, Any]]) -> list[TradePlan]:
        plans: list[TradePlan] = []
        for event_ticker, ticker_rows in _by_event_and_ticker(timeline).items():
            any_rows = next(iter(ticker_rows.values()))
            scheduled_start = any_rows[0]["scheduled_start_utc"]
            decision_time = scheduled_start - timedelta(
                minutes=self.entry_lead_minutes
            )
            candidates: list[tuple[Decimal, dict[str, Any]]] = []
            for rows in ticker_rows.values():
                candidate = _last_row_with_mid_at_or_before(rows, decision_time)
                if candidate is not None:
                    candidates.append((_mid_close(candidate), candidate))
            if not candidates:
                continue
            _, entry_row = max(candidates, key=lambda item: (item[0], item[1]["ticker"]))
            exit_not_before = scheduled_start + timedelta(
                minutes=self.live_exit_minutes
            )
            exit_row = next(
                (
                    row
                    for row in ticker_rows[entry_row["ticker"]]
                    if row["minute_end_utc"] >= exit_not_before
                    and row["game_phase"] == "live"
                ),
                None,
            )
            if exit_row is None:
                continue
            plans.append(
                _plan(
                    strategy=self,
                    index=len(plans),
                    event_ticker=event_ticker,
                    ticker=str(entry_row["ticker"]),
                    entry_row=entry_row,
                    exit_row=exit_row,
                    entry_reason=(
                        f"highest two-sided pregame midpoint at least "
                        f"{self.entry_lead_minutes} minutes before scheduled start"
                    ),
                    exit_reason=(
                        f"first live minute closing at least "
                        f"{self.live_exit_minutes} minutes after scheduled start"
                    ),
                )
            )
        return plans


@dataclass(frozen=True, slots=True)
class BuyTheDip:
    name: str = "buy_the_dip"
    lookback_minutes: int = 15
    dip_dollars: Decimal = Decimal("0.1000")
    recovery_dollars: Decimal = Decimal("0.0500")
    max_hold_minutes: int = 20

    def generate(self, timeline: list[dict[str, Any]]) -> list[TradePlan]:
        plans: list[TradePlan] = []
        for ticker, rows in _by_ticker(timeline).items():
            history: deque[tuple[datetime, Decimal]] = deque()
            entry_row: dict[str, Any] | None = None
            entry_mid: Decimal | None = None
            for row in rows:
                if row["game_phase"] != "live" or not _has_close_mid(row):
                    continue
                current_time = row["minute_end_utc"]
                current_mid = _mid_close(row)
                cutoff = current_time - timedelta(minutes=self.lookback_minutes)
                while history and history[0][0] < cutoff:
                    history.popleft()
                previous_peak = max((value for _, value in history), default=None)
                if (
                    previous_peak is not None
                    and current_mid <= previous_peak - self.dip_dollars
                ):
                    entry_row = row
                    entry_mid = current_mid
                    break
                history.append((current_time, current_mid))

            if entry_row is None or entry_mid is None:
                continue
            recovery_target = entry_mid + self.recovery_dollars
            hold_deadline = entry_row["minute_end_utc"] + timedelta(
                minutes=self.max_hold_minutes
            )
            exit_row = next(
                (
                    row
                    for row in rows
                    if row["minute_start_utc"] > entry_row["minute_start_utc"]
                    and (
                        row["game_over_observed"]
                        or row["minute_end_utc"] >= hold_deadline
                        or (
                            _has_close_mid(row)
                            and _mid_close(row) >= recovery_target
                        )
                    )
                ),
                None,
            )
            if exit_row is None:
                continue
            plans.append(
                _plan(
                    strategy=self,
                    index=len(plans),
                    event_ticker=str(entry_row["event_ticker"]),
                    ticker=ticker,
                    entry_row=entry_row,
                    exit_row=exit_row,
                    entry_reason=(
                        f"close midpoint fell at least {self.dip_dollars} below "
                        f"the prior {self.lookback_minutes}-minute peak"
                    ),
                    exit_reason=(
                        f"recovery of {self.recovery_dollars}, game over, or "
                        f"{self.max_hold_minutes}-minute maximum hold"
                    ),
                )
            )
        return plans


@dataclass(frozen=True, slots=True)
class ThreatResolution:
    name: str = "threat_resolution"
    hold_minutes: int = 10

    def generate(self, timeline: list[dict[str, Any]]) -> list[TradePlan]:
        plans: list[TradePlan] = []
        for ticker, rows in _by_ticker(timeline).items():
            threat: tuple[int, str, int] | None = None
            entry_row: dict[str, Any] | None = None
            for row in rows:
                inning = row.get("inning")
                half = row.get("half_inning")
                if row["game_phase"] != "live" or inning is None or half is None:
                    continue
                if bool(row["threat_against_yes"]):
                    if threat is None:
                        threat = (
                            int(inning),
                            str(half),
                            int(row["opponent_score"]),
                        )
                    continue
                if threat is None:
                    continue

                threat_inning, threat_half, threat_score = threat
                if int(row["opponent_score"]) > threat_score:
                    threat = None
                    continue
                changed_half = (
                    int(inning) != threat_inning or str(half) != threat_half
                )
                scoring_position_cleared = (
                    int(row["runners_in_scoring_position"]) == 0
                )
                if changed_half or scoring_position_cleared:
                    entry_row = row
                    break

            if entry_row is None:
                continue
            deadline = entry_row["minute_end_utc"] + timedelta(
                minutes=self.hold_minutes
            )
            exit_row = next(
                (
                    row
                    for row in rows
                    if row["minute_start_utc"] > entry_row["minute_start_utc"]
                    and (
                        row["game_over_observed"]
                        or row["minute_end_utc"] >= deadline
                    )
                ),
                None,
            )
            if exit_row is None:
                continue
            plans.append(
                _plan(
                    strategy=self,
                    index=len(plans),
                    event_ticker=str(entry_row["event_ticker"]),
                    ticker=ticker,
                    entry_row=entry_row,
                    exit_row=exit_row,
                    entry_reason=(
                        "opponent had a runner in scoring position, then the "
                        "threat ended without scoring"
                    ),
                    exit_reason=(
                        f"game over or {self.hold_minutes}-minute holding period"
                    ),
                )
            )
        return plans


@dataclass(frozen=True, slots=True)
class LateGameMomentum:
    name: str = "late_game_momentum"
    minimum_inning: int = 7
    max_hold_minutes: int = 30

    def generate(self, timeline: list[dict[str, Any]]) -> list[TradePlan]:
        plans: list[TradePlan] = []
        for ticker, rows in _by_ticker(timeline).items():
            entry_row = next(
                (
                    row
                    for row in rows
                    if row["game_phase"] == "live"
                    and row.get("inning") is not None
                    and int(row["inning"]) >= self.minimum_inning
                    and int(row["yes_runs_scored_this_minute"]) > 0
                    and int(row["yes_lead"]) > 0
                    and not bool(row["game_over_observed"])
                ),
                None,
            )
            if entry_row is None:
                continue
            deadline = entry_row["minute_end_utc"] + timedelta(
                minutes=self.max_hold_minutes
            )
            exit_row = next(
                (
                    row
                    for row in rows
                    if row["minute_start_utc"] > entry_row["minute_start_utc"]
                    and (
                        row["game_over_observed"]
                        or row["minute_end_utc"] >= deadline
                    )
                ),
                None,
            )
            if exit_row is None:
                continue
            plans.append(
                _plan(
                    strategy=self,
                    index=len(plans),
                    event_ticker=str(entry_row["event_ticker"]),
                    ticker=ticker,
                    entry_row=entry_row,
                    exit_row=exit_row,
                    entry_reason=(
                        f"YES team scored and led in inning "
                        f"{self.minimum_inning} or later"
                    ),
                    exit_reason=(
                        f"game over or {self.max_hold_minutes}-minute maximum hold"
                    ),
                )
            )
        return plans


def default_strategies() -> list[Strategy]:
    return [
        PregameToLive(),
        BuyTheDip(),
        ThreatResolution(),
        LateGameMomentum(),
    ]


def generate_trade_plans(
    timeline: list[dict[str, Any]], strategies: list[Strategy] | None = None
) -> list[TradePlan]:
    plans = [
        plan
        for strategy in (strategies or default_strategies())
        for plan in strategy.generate(timeline)
    ]
    return sorted(
        plans,
        key=lambda plan: (
            plan.strategy,
            plan.entry_signal_after_utc,
            plan.event_ticker,
            plan.ticker,
        ),
    )


def _plan(
    *,
    strategy: Any,
    index: int,
    event_ticker: str,
    ticker: str,
    entry_row: dict[str, Any],
    exit_row: dict[str, Any],
    entry_reason: str,
    exit_reason: str,
) -> TradePlan:
    if exit_row["minute_end_utc"] <= entry_row["minute_end_utc"]:
        raise ValueError("exit signal must be observed after entry signal")
    return TradePlan(
        plan_id=f"{strategy.name}:{event_ticker}:{ticker}:{index:04d}",
        strategy=strategy.name,
        event_ticker=event_ticker,
        ticker=ticker,
        entry_signal_minute_start_utc=entry_row["minute_start_utc"],
        entry_signal_after_utc=entry_row["minute_end_utc"],
        exit_signal_minute_start_utc=exit_row["minute_start_utc"],
        exit_signal_after_utc=exit_row["minute_end_utc"],
        entry_reason=entry_reason,
        exit_reason=exit_reason,
        parameters={
            key: str(value) if isinstance(value, Decimal) else value
            for key, value in asdict(strategy).items()
            if key != "name"
        },
    )


def _by_ticker(
    timeline: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    grouped: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in timeline:
        grouped[str(row["ticker"])].append(row)
    for rows in grouped.values():
        rows.sort(key=lambda row: row["minute_start_utc"])
    return dict(grouped)


def _by_event_and_ticker(
    timeline: list[dict[str, Any]],
) -> dict[str, dict[str, list[dict[str, Any]]]]:
    grouped: defaultdict[str, defaultdict[str, list[dict[str, Any]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for row in timeline:
        grouped[str(row["event_ticker"])][str(row["ticker"])].append(row)
    return {
        event: {
            ticker: sorted(rows, key=lambda row: row["minute_start_utc"])
            for ticker, rows in tickers.items()
        }
        for event, tickers in grouped.items()
    }


def _last_row_with_mid_at_or_before(
    rows: list[dict[str, Any]], cutoff: datetime
) -> dict[str, Any] | None:
    candidates = [
        row
        for row in rows
        if row["minute_end_utc"] <= cutoff and _has_close_mid(row)
    ]
    return candidates[-1] if candidates else None


def _has_close_mid(row: dict[str, Any]) -> bool:
    return row.get("yes_bid_close") is not None and row.get("yes_ask_close") is not None


def _mid_close(row: dict[str, Any]) -> Decimal:
    bid = row.get("yes_bid_close")
    ask = row.get("yes_ask_close")
    if not isinstance(bid, Decimal) or not isinstance(ask, Decimal):
        raise ValueError("two-sided close quote required")
    return (bid + ask) / Decimal(2)
