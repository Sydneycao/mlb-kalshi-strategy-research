# MLB Kalshi historical market research

This Python 3.12 project provides a production-oriented ingestion foundation for
researching settled `KXMLBGAME` markets. The current scope is deliberately narrow:

- an API availability probe;
- a configurable, bounded smoke test (10 games by default);
- raw-response preservation, normalized Parquet output, and deterministic game
  matching.

It contains no trading strategy, order placement, credentialed endpoint, or runtime
LLM dependency.

## Data sources

Kalshi's public market-data API is queried at
`https://external-api.kalshi.com/trade-api/v2`. The ingestion first reads the
[historical cutoff](https://docs.kalshi.com/getting_started/historical_data), then:

- discovers recent settled markets from `GET /markets`;
- discovers archived settled markets from `GET /historical/markets`;
- routes one-minute candles to the live or historical market endpoint based on the
  market tier;
- splits each ticker's trade window at `trades_created_ts`, querying current and
  historical trades as applicable.

MLB schedules come from `/api/v1/schedule`, and matched games' complete play-by-play
feeds come from `/api/v1.1/game/{gamePk}/feed/live`.

All requests have cursor pagination where supported, bounded retries, exponential
jitter, `Retry-After` handling for rate limits, timeouts, and one-line JSON logs.

## Quick start

Install and select CPython 3.12:

```bash
uv python install 3.12
uv sync --python 3.12 --extra dev
```

Probe every required API family with small requests:

```bash
uv run --python 3.12 mlb-kalshi probe
```

Run the first smoke test:

```bash
uv run --python 3.12 mlb-kalshi smoke --max-games 10
```

The default is also configurable through `MLB_KALSHI_MAX_GAMES`. See
[`.env.example`](.env.example) for the complete environment-variable surface.

## Matching rules

The smoke test groups Kalshi contracts by `event_ticker`, so the two complementary
team contracts count as one baseball game. A match must pass, in order:

1. both team names normalize to the same unordered MLB team pair;
2. the date encoded in the Kalshi event ticker equals MLB `officialDate`;
3. the Eastern-time scheduled start encoded in that ticker, normalized to UTC, is
   the uniquely nearest MLB scheduled time inside the configured tolerance;
4. the Kalshi settled winner equals the winner implied by the final MLB score.

This ordering prevents a second game in a doubleheader from being selected merely
because its result happens to agree. Unmatched events are not silently discarded.
Each gets a reason code, human-readable explanation, candidate counts, schedule
time deltas, and result details in both `game_rejections.parquet` and
`rejections.json`.

## Output layout

Every run receives a UTC run ID:

```text
data/
├── raw/<run_id>/             # exact API JSON payloads, separated by provider/endpoint
├── normalized/<run_id>/      # typed, UTC-normalized Parquet datasets
└── runs/<run_id>/            # manifest, counts, errors, rejection details
```

Normalized smoke outputs are:

- `kalshi_markets.parquet`
- `kalshi_candlesticks_1m.parquet`
- `kalshi_trades.parquet`
- `mlb_schedule.parquet`
- `mlb_plays.parquet`
- `game_matches.parquet`
- `game_rejections.parquet`

All timestamp columns are timezone-aware UTC Arrow timestamps. Fixed-point Kalshi
prices, counts, and volume remain strings so ingestion cannot introduce binary
floating-point error.

`data/raw`, `data/normalized`, `data/runs`, `.env*`, private-key formats, and common
credential directories are ignored by Git. Never force-add downloaded data or
credentials.

## Verification

```bash
uv run --python 3.12 ruff check .
uv run --python 3.12 pytest
```

The tests cover team aliases, UTC conversion, doubleheader disambiguation, final
result matching, cursor pagination, and rate-limit retry behavior. The manual
`workflow_dispatch` GitHub Actions workflow runs lint/tests and optionally runs the
public API probe plus bounded live smoke test.
