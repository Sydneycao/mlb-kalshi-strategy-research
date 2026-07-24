from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import quote

import structlog

from mlb_kalshi.http import JsonObject, ResilientJsonClient
from mlb_kalshi.models import KalshiGame, MarketSource
from mlb_kalshi.normalize import build_kalshi_game
from mlb_kalshi.storage import RawStore
from mlb_kalshi.time import optional_utc, unix_seconds

SERIES_TICKER = "KXMLBGAME"


class KalshiClient:
    def __init__(
        self,
        http: ResilientJsonClient,
        raw_store: RawStore,
        *,
        page_size: int,
    ) -> None:
        self._http = http
        self._raw = raw_store
        self._page_size = page_size
        self._log = structlog.get_logger("kalshi")

    def get_cutoff(self, *, raw_section: str = "cutoff") -> JsonObject:
        payload = self._http.get_json("/historical/cutoff")
        self._raw.write_json("kalshi", raw_section, "response", payload=payload)
        return payload

    def list_settled_markets(
        self,
        source: MarketSource,
        *,
        max_pages: int,
        target_events: int | None = None,
        raw_section: str | None = None,
        page_size: int | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        path = "/markets" if source == "current" else "/historical/markets"
        params: dict[str, Any] = {
            "series_ticker": SERIES_TICKER,
            "limit": page_size or self._page_size,
        }
        if source == "current":
            params["status"] = "settled"
            params["mve_filter"] = "exclude"
        section = raw_section or f"{source}_markets"
        seen_events: set[str] = set()

        def stop(items: list[dict[str, Any]]) -> bool:
            seen_events.update(
                str(item.get("event_ticker"))
                for item in items
                if item.get("event_ticker")
            )
            return target_events is not None and len(seen_events) >= target_events

        items, metadata = self._paginate(
            path,
            params=params,
            item_key="markets",
            raw_section=section,
            max_pages=max_pages,
            stop=stop,
        )
        settled = [
            market
            for market in items
            if str(market.get("event_ticker", "")).startswith(f"{SERIES_TICKER}-")
            and str(market.get("result", "")).lower() in {"yes", "no"}
        ]
        metadata["settled_items"] = len(settled)
        metadata["event_count"] = len(
            {market.get("event_ticker") for market in settled}
        )
        return settled, metadata

    def discover_games(
        self,
        *,
        max_games: int,
        max_pages: int,
    ) -> tuple[list[KalshiGame], dict[str, Any]]:
        requested_current = (max_games + 1) // 2
        requested_historical = max_games // 2
        if max_games == 1:
            # Still probe and discover both tiers; selection remains bounded to one game.
            requested_historical = 1

        by_source: dict[MarketSource, list[KalshiGame]] = {}
        metadata: dict[str, Any] = {}
        for source in ("current", "historical"):
            target = requested_current if source == "current" else requested_historical
            markets, source_metadata = self.list_settled_markets(
                source,
                max_pages=max_pages,
                target_events=max(1, target),
            )
            by_event: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
            for market in markets:
                event_ticker = market.get("event_ticker")
                if isinstance(event_ticker, str):
                    by_event[event_ticker].append(market)
            games = [
                build_kalshi_game(event_ticker, source, grouped)
                for event_ticker, grouped in by_event.items()
            ]
            games.sort(key=_game_sort_key, reverse=True)
            by_source[source] = games
            metadata[source] = source_metadata

        selected = (
            by_source["current"][:requested_current]
            + by_source["historical"][:requested_historical]
        )
        if len(selected) < max_games:
            selected_events = {game.event_ticker for game in selected}
            remaining = sorted(
                (
                    game
                    for games in by_source.values()
                    for game in games
                    if game.event_ticker not in selected_events
                ),
                key=_game_sort_key,
                reverse=True,
            )
            selected.extend(remaining[: max_games - len(selected)])

        selected = selected[:max_games]
        metadata["selected"] = {
            "total": len(selected),
            "current": sum(game.source == "current" for game in selected),
            "historical": sum(game.source == "historical" for game in selected),
        }
        self._log.info("kalshi_games_discovered", **metadata["selected"])
        return selected, metadata

    def get_candlesticks(
        self,
        *,
        market: dict[str, Any],
        source: MarketSource,
        start: datetime,
        end: datetime,
        raw_section: str,
    ) -> JsonObject:
        ticker = quote(str(market["ticker"]), safe="")
        if source == "historical":
            path = f"/historical/markets/{ticker}/candlesticks"
        else:
            path = f"/series/{SERIES_TICKER}/markets/{ticker}/candlesticks"
        payload = self._http.get_json(
            path,
            params={
                "start_ts": unix_seconds(start),
                "end_ts": unix_seconds(end),
                "period_interval": 1,
            },
        )
        self._raw.write_json(
            "kalshi", raw_section, "candlesticks", str(market["ticker"]), payload=payload
        )
        return payload

    def get_trades(
        self,
        *,
        ticker: str,
        source: Literal["current", "historical"],
        start: datetime,
        end: datetime,
        max_pages: int,
        raw_section: str,
        page_size: int | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        path = "/markets/trades" if source == "current" else "/historical/trades"
        return self._paginate(
            path,
            params={
                "ticker": ticker,
                "min_ts": unix_seconds(start),
                "max_ts": unix_seconds(end),
                "limit": page_size or self._page_size,
            },
            item_key="trades",
            raw_section=f"{raw_section}_trades_{source}_{ticker}",
            max_pages=max_pages,
        )

    def _paginate(
        self,
        path: str,
        *,
        params: dict[str, Any],
        item_key: str,
        raw_section: str,
        max_pages: int,
        stop: Callable[[list[dict[str, Any]]], bool] | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        items: list[dict[str, Any]] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        truncated = False
        pages = 0

        for page_number in range(1, max_pages + 1):
            request_params = dict(params)
            if cursor:
                request_params["cursor"] = cursor
            payload = self._http.get_json(path, params=request_params)
            self._raw.write_json(
                "kalshi",
                raw_section,
                f"page_{page_number:04d}",
                payload=payload,
            )
            page_items = payload.get(item_key, [])
            if not isinstance(page_items, list):
                raise ValueError(f"{path} response field {item_key!r} is not a list")
            typed_items = [item for item in page_items if isinstance(item, dict)]
            items.extend(typed_items)
            pages = page_number

            cursor_value = payload.get("cursor")
            next_cursor = cursor_value if isinstance(cursor_value, str) else ""
            if stop is not None and stop(typed_items):
                truncated = bool(next_cursor)
                break
            if not next_cursor:
                break
            if next_cursor in seen_cursors:
                raise RuntimeError(f"{path} returned a repeated pagination cursor")
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        else:
            truncated = bool(cursor)

        metadata = {
            "path": path,
            "pages": pages,
            "items": len(items),
            "truncated": truncated,
            "max_pages": max_pages,
        }
        if truncated:
            self._log.warning("pagination_truncated", **metadata)
        else:
            self._log.info("pagination_complete", **metadata)
        return items, metadata


def market_window(market: dict[str, Any]) -> tuple[datetime, datetime] | None:
    start = optional_utc(market.get("open_time"))
    end = (
        optional_utc(market.get("settlement_ts"))
        or optional_utc(market.get("close_time"))
        or optional_utc(market.get("expiration_time"))
    )
    if start is None or end is None or end < start:
        return None
    return start, end


def _game_sort_key(game: KalshiGame) -> datetime:
    return game.scheduled_start_utc or datetime.min.replace(tzinfo=UTC)
