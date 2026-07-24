import json

import httpx

from mlb_kalshi.http import ResilientJsonClient


def test_rate_limit_response_honors_retry_after() -> None:
    calls = 0
    slept: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": "1.25"}, request=request)
        return httpx.Response(
            200,
            content=json.dumps({"ok": True}).encode(),
            headers={"Content-Type": "application/json"},
            request=request,
        )

    with ResilientJsonClient(
        base_url="https://example.test",
        timeout_seconds=1,
        max_attempts=2,
        backoff_base_seconds=0.1,
        backoff_cap_seconds=10,
        user_agent="test",
        transport=httpx.MockTransport(handler),
        sleep=slept.append,
    ) as client:
        assert client.get_json("/resource") == {"ok": True}

    assert calls == 2
    assert slept == [1.25]
