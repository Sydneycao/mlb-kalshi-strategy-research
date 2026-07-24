from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    return default if value is None else int(value)


def _env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    return default if value is None else float(value)


@dataclass(frozen=True, slots=True)
class Settings:
    """Runtime settings for the bounded research ingestion."""

    kalshi_base_url: str = "https://external-api.kalshi.com/trade-api/v2"
    mlb_base_url: str = "https://statsapi.mlb.com"
    output_dir: Path = Path("data")
    max_games: int = 10
    page_size: int = 1000
    max_discovery_pages: int = 20
    max_trade_pages: int = 100
    http_timeout_seconds: float = 30.0
    max_attempts: int = 5
    backoff_base_seconds: float = 0.5
    backoff_cap_seconds: float = 30.0
    match_tolerance_minutes: int = 120
    schedule_buffer_days: int = 1
    log_level: str = "INFO"
    user_agent: str = "mlb-kalshi-research/0.1 (+historical-market-research)"

    @classmethod
    def from_env(cls) -> Settings:
        defaults = cls()
        settings = cls(
            kalshi_base_url=os.getenv(
                "MLB_KALSHI_KALSHI_BASE_URL", defaults.kalshi_base_url
            ).rstrip("/"),
            mlb_base_url=os.getenv(
                "MLB_KALSHI_MLB_BASE_URL", defaults.mlb_base_url
            ).rstrip("/"),
            output_dir=Path(
                os.getenv("MLB_KALSHI_OUTPUT_DIR", str(defaults.output_dir))
            ),
            max_games=_env_int("MLB_KALSHI_MAX_GAMES", defaults.max_games),
            page_size=_env_int("MLB_KALSHI_PAGE_SIZE", defaults.page_size),
            max_discovery_pages=_env_int(
                "MLB_KALSHI_MAX_DISCOVERY_PAGES", defaults.max_discovery_pages
            ),
            max_trade_pages=_env_int(
                "MLB_KALSHI_MAX_TRADE_PAGES", defaults.max_trade_pages
            ),
            http_timeout_seconds=_env_float(
                "MLB_KALSHI_HTTP_TIMEOUT_SECONDS", defaults.http_timeout_seconds
            ),
            max_attempts=_env_int(
                "MLB_KALSHI_MAX_ATTEMPTS", defaults.max_attempts
            ),
            backoff_base_seconds=_env_float(
                "MLB_KALSHI_BACKOFF_BASE_SECONDS", defaults.backoff_base_seconds
            ),
            backoff_cap_seconds=_env_float(
                "MLB_KALSHI_BACKOFF_CAP_SECONDS", defaults.backoff_cap_seconds
            ),
            match_tolerance_minutes=_env_int(
                "MLB_KALSHI_MATCH_TOLERANCE_MINUTES",
                defaults.match_tolerance_minutes,
            ),
            schedule_buffer_days=_env_int(
                "MLB_KALSHI_SCHEDULE_BUFFER_DAYS", defaults.schedule_buffer_days
            ),
            log_level=os.getenv("MLB_KALSHI_LOG_LEVEL", defaults.log_level),
        )
        settings.validate()
        return settings

    def with_overrides(
        self, *, max_games: int | None = None, output_dir: Path | None = None
    ) -> Settings:
        settings = replace(
            self,
            max_games=self.max_games if max_games is None else max_games,
            output_dir=self.output_dir if output_dir is None else output_dir,
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        if not 1 <= self.max_games <= 100:
            raise ValueError("max_games must be between 1 and 100")
        if not 1 <= self.page_size <= 1000:
            raise ValueError("page_size must be between 1 and 1000")
        if self.max_discovery_pages < 1 or self.max_trade_pages < 1:
            raise ValueError("page limits must be positive")
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        if self.http_timeout_seconds <= 0:
            raise ValueError("http_timeout_seconds must be positive")
        if self.match_tolerance_minutes < 0:
            raise ValueError("match_tolerance_minutes cannot be negative")
