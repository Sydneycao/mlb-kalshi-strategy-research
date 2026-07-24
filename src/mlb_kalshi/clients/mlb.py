from __future__ import annotations

from datetime import date
from typing import Any

from mlb_kalshi.http import JsonObject, ResilientJsonClient
from mlb_kalshi.storage import RawStore


class MlbClient:
    def __init__(self, http: ResilientJsonClient, raw_store: RawStore) -> None:
        self._http = http
        self._raw = raw_store

    def get_schedule(
        self,
        *,
        start_date: date,
        end_date: date,
        raw_section: str = "schedule",
    ) -> JsonObject:
        payload = self._http.get_json(
            "/api/v1/schedule",
            params={
                "sportId": 1,
                "startDate": start_date.isoformat(),
                "endDate": end_date.isoformat(),
                "hydrate": "team,linescore",
            },
        )
        self._raw.write_json("mlb", raw_section, "response", payload=payload)
        return payload

    def get_play_by_play(
        self, game_pk: int, *, raw_section: str = "play_by_play"
    ) -> JsonObject:
        payload = self._http.get_json(f"/api/v1.1/game/{game_pk}/feed/live")
        self._raw.write_json(
            "mlb", raw_section, f"game_{game_pk}", payload=payload
        )
        return payload

    @staticmethod
    def first_game_pk(payload: dict[str, Any]) -> int | None:
        for date_entry in payload.get("dates", []):
            for game in date_entry.get("games", []):
                game_pk = game.get("gamePk")
                if isinstance(game_pk, int):
                    return game_pk
        return None
