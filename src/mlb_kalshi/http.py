from __future__ import annotations

import email.utils
import random
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

import httpx
import structlog

JsonObject = dict[str, Any]

_RETRYABLE_STATUS_CODES = {408, 409, 425, 429, 500, 502, 503, 504}


class ApiRequestError(RuntimeError):
    """A public API request failed after applying the retry policy."""


class ResilientJsonClient:
    def __init__(
        self,
        *,
        base_url: str,
        timeout_seconds: float,
        max_attempts: int,
        backoff_base_seconds: float,
        backoff_cap_seconds: float,
        user_agent: str,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        random_source: random.Random | None = None,
    ) -> None:
        self._client = httpx.Client(
            base_url=base_url,
            timeout=timeout_seconds,
            headers={"Accept": "application/json", "User-Agent": user_agent},
            follow_redirects=True,
            transport=transport,
        )
        self._max_attempts = max_attempts
        self._backoff_base = backoff_base_seconds
        self._backoff_cap = backoff_cap_seconds
        self._sleep = sleep
        self._random = random_source or random.Random()
        self._log = structlog.get_logger("http")

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> ResilientJsonClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def get_json(
        self, path: str, *, params: Mapping[str, Any] | None = None
    ) -> JsonObject:
        clean_params = {
            key: value
            for key, value in (params or {}).items()
            if value is not None and value != ""
        }
        last_error: Exception | None = None

        for attempt in range(1, self._max_attempts + 1):
            started = time.monotonic()
            try:
                response = self._client.get(path, params=clean_params)
                elapsed_ms = round((time.monotonic() - started) * 1000, 1)
                if response.status_code in _RETRYABLE_STATUS_CODES:
                    if attempt == self._max_attempts:
                        response.raise_for_status()
                    delay = self._retry_delay(response, attempt)
                    self._log.warning(
                        "api_retry",
                        method="GET",
                        path=path,
                        status_code=response.status_code,
                        attempt=attempt,
                        delay_seconds=round(delay, 3),
                        elapsed_ms=elapsed_ms,
                    )
                    self._sleep(delay)
                    continue

                if response.is_error:
                    body = response.text[:1000].replace("\n", " ")
                    raise ApiRequestError(
                        f"GET {path} returned HTTP {response.status_code}: {body}"
                    )
                payload = response.json()
                if not isinstance(payload, dict):
                    raise ApiRequestError(f"GET {path} returned a non-object JSON payload")
                self._log.info(
                    "api_request",
                    method="GET",
                    path=path,
                    status_code=response.status_code,
                    attempt=attempt,
                    elapsed_ms=elapsed_ms,
                    rate_limit_remaining=response.headers.get("x-ratelimit-remaining"),
                )
                return payload
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                last_error = exc
                if attempt == self._max_attempts:
                    break
                delay = self._exponential_delay(attempt)
                self._log.warning(
                    "api_transport_retry",
                    method="GET",
                    path=path,
                    attempt=attempt,
                    delay_seconds=round(delay, 3),
                    error_type=type(exc).__name__,
                )
                self._sleep(delay)
            except (httpx.HTTPStatusError, ValueError) as exc:
                raise ApiRequestError(f"GET {path} failed: {exc}") from exc

        raise ApiRequestError(
            f"GET {path} failed after {self._max_attempts} attempts: {last_error}"
        ) from last_error

    def _retry_delay(self, response: httpx.Response, attempt: int) -> float:
        retry_after = response.headers.get("retry-after")
        if retry_after:
            try:
                return min(self._backoff_cap, max(0.0, float(retry_after)))
            except ValueError:
                parsed = email.utils.parsedate_to_datetime(retry_after)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=UTC)
                seconds = (parsed.astimezone(UTC) - datetime.now(UTC)).total_seconds()
                return min(self._backoff_cap, max(0.0, seconds))
        return self._exponential_delay(attempt)

    def _exponential_delay(self, attempt: int) -> float:
        ceiling = min(self._backoff_cap, self._backoff_base * (2 ** (attempt - 1)))
        return self._random.uniform(0, ceiling)
