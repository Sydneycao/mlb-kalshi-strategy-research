from mlb_kalshi.normalize import normalize_candlesticks


def test_historical_candlestick_legacy_fields_are_normalized() -> None:
    rows = normalize_candlesticks(
        event_ticker="KXMLBGAME-26MAY231605CWSSF",
        market_ticker="KXMLBGAME-26MAY231605CWSSF-CWS",
        source="historical",
        payload={
            "candlesticks": [
                {
                    "end_period_ts": 1_779_308_760,
                    "volume": "12.50",
                    "open_interest": "7.25",
                    "yes_bid": {
                        "open": "0.3500",
                        "low": "0.3400",
                        "high": "0.3700",
                        "close": "0.3600",
                    },
                    "yes_ask": {
                        "open": "0.3900",
                        "low": "0.3800",
                        "high": "0.4100",
                        "close": "0.4000",
                    },
                    "price": {
                        "open": "0.3700",
                        "low": "0.3600",
                        "high": "0.3900",
                        "close": "0.3800",
                    },
                }
            ]
        },
    )

    assert rows[0]["yes_bid_open_dollars"] == "0.3500"
    assert rows[0]["yes_ask_close_dollars"] == "0.4000"
    assert rows[0]["price_high_dollars"] == "0.3900"
    assert rows[0]["volume_fp"] == "12.50"
    assert rows[0]["open_interest_fp"] == "7.25"
