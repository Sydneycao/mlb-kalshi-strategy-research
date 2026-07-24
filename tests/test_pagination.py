from typing import Any

from mlb_kalshi.clients.kalshi import KalshiClient
from mlb_kalshi.storage import RawStore


class FakeHttp:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def get_json(self, path: str, *, params: dict[str, Any]) -> dict[str, Any]:
        self.calls.append({"path": path, "params": params})
        if len(self.calls) == 1:
            return {
                "markets": [_market("ONE")],
                "cursor": "next-page",
            }
        return {"markets": [_market("TWO")], "cursor": ""}


def _market(suffix: str) -> dict[str, Any]:
    return {
        "event_ticker": f"KXMLBGAME-25JUL121310BOSNYY{suffix}",
        "ticker": f"KXMLBGAME-25JUL121310BOSNYY{suffix}-BOS",
        "result": "yes",
    }


def test_market_discovery_follows_cursor(tmp_path: Any) -> None:
    http = FakeHttp()
    client = KalshiClient(  # type: ignore[arg-type]
        http,
        RawStore(tmp_path),
        page_size=1,
    )

    markets, metadata = client.list_settled_markets(
        "historical", max_pages=3, target_events=None
    )

    assert len(markets) == 2
    assert metadata["pages"] == 2
    assert not metadata["truncated"]
    assert http.calls[1]["params"]["cursor"] == "next-page"
