from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from mlb_kalshi.clients.kalshi import (
    MAX_CANDLE_WINDOW_MINUTES,
    KalshiClient,
)
from mlb_kalshi.storage import RawStore


class FakeHttp:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def get_json(self, path: str, *, params: dict[str, Any]) -> dict[str, Any]:
        self.calls.append({"path": path, "params": params})
        return {
            "candlesticks": [
                {
                    "end_period_ts": params["end_ts"],
                    "volume_fp": "1.00",
                }
            ]
        }


def test_long_candlestick_window_is_chunked_below_api_limit(
    tmp_path: Path,
) -> None:
    http = FakeHttp()
    client = KalshiClient(  # type: ignore[arg-type]
        http,
        RawStore(tmp_path),
        page_size=1000,
    )
    start = datetime(2025, 1, 1, tzinfo=UTC)
    end = start + timedelta(minutes=MAX_CANDLE_WINDOW_MINUTES * 2 + 1)

    payload = client.get_candlesticks(
        market={"ticker": "KXMLBGAME-TEST-BOS"},
        source="historical",
        start=start,
        end=end,
        raw_section="test",
    )

    assert len(http.calls) == 3
    assert len(payload["candlesticks"]) == 3
    assert all(
        call["params"]["end_ts"] - call["params"]["start_ts"]
        <= MAX_CANDLE_WINDOW_MINUTES * 60
        for call in http.calls
    )
