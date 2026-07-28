# MLB Kalshi historical market research

This Python 3.12 project provides a production-oriented ingestion foundation for
researching settled `KXMLBGAME` markets. It includes:

- an API availability probe;
- a configurable, bounded smoke test (10 games by default);
- a resumable historical backfill (500 games by default);
- raw-response preservation, normalized Parquet output, and deterministic game
  matching.

It contains no trading strategy, order placement, credentialed endpoint, or runtime
LLM dependency in the ingestion stage. The research stage described below simulates
historical signals only; it never places orders.

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

Start a historical batch backfill. A stable `job-id` is the checkpoint name:

```bash
uv run --python 3.12 mlb-kalshi backfill \
  --job-id mlb-2025 \
  --start-date 2025-03-18 \
  --end-date 2025-09-28 \
  --max-games 500 \
  --batch-size 25
```

If the process is interrupted, run the same job again. The saved dates, game
limit, and batch size are reused, so they do not need to be repeated:

```bash
uv run --python 3.12 mlb-kalshi backfill --job-id mlb-2025
```

Completed games are skipped. Failed or interrupted games are retried. To process a
bounded number of games per scheduled invocation, add
`--max-games-this-run 25`; the next invocation continues from the following
checkpoint.

The smoke-test default is also configurable through `MLB_KALSHI_MAX_GAMES`. See
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

Smoke runs use timestamped IDs. Backfills use the stable
`backfill_<job-id>` ID and add:

```text
data/runs/backfill_<job-id>/
├── state.json                # per-schedule-chunk and per-game checkpoint state
├── catalog.json              # fixed Kalshi event catalog for this task
├── chunks/
│   ├── schedule/             # 31-day MLB schedule checkpoints
│   └── games/                # one atomic normalized checkpoint per event
├── manifest.json             # status: partial, needs_retry, empty, or completed
└── rejections.json
```

Every 25 attempts by default, and again before exit, completed chunks are
deduplicated into the same normalized Parquet files used by a smoke run. A
`needs_retry` task returns a non-zero exit code; rerunning the same command retries
only unfinished resources. Resume arguments that conflict with the saved task
configuration are rejected instead of silently mixing date ranges.

Normalized ingestion outputs are:

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
public API probe, bounded live smoke test, and bias-safe backtest.

## Minute timeline and strategy research

After a successful smoke run or completed backfill, build the unified timeline and
simulate all four initial strategy definitions:

```bash
uv run --python 3.12 mlb-kalshi backtest
```

The latest completed ingestion run is selected automatically. A specific run and
subset of strategies can be selected explicitly:

```bash
uv run --python 3.12 mlb-kalshi backtest \
  --input-run smoke_20260724T022147.632330Z \
  --strategies buy_the_dip,threat_resolution \
  --contracts-per-trade 10 \
  --max-volume-participation 0.10
```

For the example backfill, use `--input-run backfill_mlb-2025`. Partial or
`needs_retry` backfills cannot be used for a backtest.

Each market gets a continuous minute grid from three hours before scheduled first
pitch through settlement. A row represents `[minute_start_utc, minute_end_utc)`.
MLB events with timestamps inside that interval update game state only at
`minute_end_utc`. Quotes are never forward-filled.

The execution engine enforces these rules for every strategy:

- a signal observed in minute `t` becomes available only when `t` closes;
- a purchase uses the first non-null YES ask **open** at or after that close;
- a sale uses the first non-null YES bid **open** at or after its signal closes;
- same-minute high, low, close, and trade prices are never executable;
- missing-quote minutes, bid/ask spreads, execution timestamps, and delays are
  retained for both legs;
- orders are all-or-none for the configured contract quantity;
- executable capacity is capped at the configured share of same-minute public
  trade volume at the quote price or better; an undersized minute is skipped;
- both legs pay the `KXMLBGAME` quadratic taker fee
  `ceil_cent($0.07 × contracts × price × (1 - price))`;
- results are emitted for base execution and one-cent adverse slippage
  (`buy + $0.01`, `sell - $0.01`).

The default request is one contract and the default maximum volume participation
is 10%. Public trades are an ex-post capacity proxy, not historical order-book
depth, so a capacity-qualified fill is still a simulation. The fee model uses the
`KXMLBGAME` taker multiplier of 1 and conservative whole-cent retail rounding on
each all-or-none leg. Gross PnL, entry/exit fees, total fees, and net PnL are all
retained; summaries use net PnL. Direct exchange members can select centicent
rounding with `--fee-rounding-quantum 0.0001`.

The initial strategy modules are deliberately fixed rather than optimized:

- **Pregame-to-Live** buys the contract with the highest two-sided midpoint five
  minutes before scheduled start and exits after 15 live minutes.
- **Buy the Dip** buys after a 10-cent midpoint decline from the prior 15-minute
  peak and exits on a five-cent recovery, game end, or 20-minute maximum hold.
- **Threat Resolution** buys the defending team after an opponent's runner-in-
  scoring-position threat ends without a run and holds for 10 minutes or game end.
- **Late-Game Momentum** buys a team that scores to lead in inning seven or later
  and exits at game end or after 30 minutes.

Research outputs are saved under `data/research/<run_id>/`:

- `minute_game_market_timeline.parquet`
- `strategy_signals.parquet`
- `strategy_executions.parquet`
- `strategy_summary.parquet`
- `strategy_summary.csv`
- `strategy_report.md`
- `manifest.json`

These are smoke-sample diagnostics, not evidence that a strategy is profitable.
Parameter search and out-of-sample evaluation should happen only after timeline,
fee, and executable-capacity audits pass.
