from __future__ import annotations

import json
import re
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pyarrow as pa  # type: ignore[import-untyped]
import pyarrow.parquet as pq  # type: ignore[import-untyped]
import structlog

from mlb_kalshi.clients.kalshi import KalshiClient, market_window
from mlb_kalshi.clients.mlb import MlbClient
from mlb_kalshi.config import Settings
from mlb_kalshi.matching import match_game
from mlb_kalshi.models import KalshiGame, MarketSource, MatchedGame, MlbGame
from mlb_kalshi.normalize import (
    build_kalshi_game,
    normalize_candlesticks,
    normalize_mlb_schedule,
    normalize_plays,
    normalize_trades,
)
from mlb_kalshi.pipeline import (
    _ClientContext,
    _market_row,
    _match_row,
    _mlb_game_row,
    _rejection_row,
)
from mlb_kalshi.storage import (
    CANDLE_SCHEMA,
    MARKET_SCHEMA,
    MATCH_SCHEMA,
    MLB_GAME_SCHEMA,
    PLAY_SCHEMA,
    REJECTION_SCHEMA,
    TRADE_SCHEMA,
    NormalizedStore,
    RawStore,
    RunLayout,
    write_manifest,
)
from mlb_kalshi.time import optional_utc, parse_utc, utc_iso

DEFAULT_MAX_GAMES = 500
DEFAULT_BATCH_SIZE = 25
SCHEDULE_CHUNK_DAYS = 31
_JOB_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SAFE_SEGMENT = re.compile(r"[^A-Za-z0-9_.=-]+")

_DATASETS: dict[str, tuple[str, pa.Schema]] = {
    "markets": ("kalshi_markets", MARKET_SCHEMA),
    "candlesticks": ("kalshi_candlesticks_1m", CANDLE_SCHEMA),
    "trades": ("kalshi_trades", TRADE_SCHEMA),
    "mlb_plays": ("mlb_plays", PLAY_SCHEMA),
    "matches": ("game_matches", MATCH_SCHEMA),
    "rejections": ("game_rejections", REJECTION_SCHEMA),
}


class HistoricalBackfillPipeline:
    """Resumable, game-checkpointed ingestion for hundreds of settled MLB events."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._log = structlog.get_logger("backfill")

    def run(
        self,
        *,
        job_id: str,
        start_date: date | None,
        end_date: date | None,
        max_games: int | None,
        batch_size: int | None,
        max_games_this_run: int | None = None,
    ) -> dict[str, Any]:
        _validate_job_id(job_id)
        if max_games_this_run is not None and max_games_this_run < 1:
            raise ValueError("max_games_this_run must be positive")

        run_id = f"backfill_{job_id}"
        layout = RunLayout(self.settings.output_dir, run_id)
        state_path = layout.run_dir / "state.json"
        resumed = state_path.is_file()
        if resumed:
            state = _read_json(state_path)
            configuration = _resume_configuration(
                state,
                start_date=start_date,
                end_date=end_date,
                max_games=max_games,
                batch_size=batch_size,
            )
            layout.create_or_resume()
        else:
            configuration = _new_configuration(
                start_date=start_date,
                end_date=end_date,
                max_games=max_games,
                batch_size=batch_size,
            )
            if layout.raw_dir.exists() or layout.normalized_dir.exists() or layout.run_dir.exists():
                raise ValueError(
                    f"backfill directories already exist without a checkpoint: {run_id}"
                )
            layout.create()
            now = utc_iso(datetime.now(UTC))
            state = {
                "schema_version": 1,
                "run_id": run_id,
                "job_id": job_id,
                "status": "initialized",
                "created_at_utc": now,
                "updated_at_utc": now,
                "configuration": configuration,
                "discovery": {"status": "pending"},
                "schedule_chunks": {},
                "games": {},
                "error_history": [],
                "last_error": None,
            }
            _write_state(state_path, state)

        state["last_invocation_started_at_utc"] = utc_iso(datetime.now(UTC))
        state["last_invocation_resumed"] = resumed
        state["last_error"] = None
        state["status"] = "running"
        _write_state(state_path, state)

        raw = RawStore(layout.raw_dir)
        catalog_games: list[KalshiGame] = []
        attempted_this_run = 0
        completed_this_run = 0
        failed_this_run = 0
        batches_written = 0

        try:
            with self._clients(raw) as (kalshi, mlb):
                cutoff = kalshi.get_cutoff(raw_section="backfill_cutoff")
                state["last_cutoff"] = cutoff
                _write_state(state_path, state)

                catalog_games = self._load_or_discover_catalog(
                    layout=layout,
                    state=state,
                    state_path=state_path,
                    kalshi=kalshi,
                    configuration=configuration,
                )
                self._initialize_game_checkpoints(state, catalog_games)
                _write_state(state_path, state)

                if catalog_games:
                    mlb_games = self._load_or_fetch_schedules(
                        layout=layout,
                        state=state,
                        state_path=state_path,
                        mlb=mlb,
                        configuration=configuration,
                    )
                    batch_progress = 0
                    for catalog_game in catalog_games:
                        checkpoint = state["games"][catalog_game.event_ticker]
                        if checkpoint.get("status") == "completed":
                            continue
                        if (
                            max_games_this_run is not None
                            and attempted_this_run >= max_games_this_run
                        ):
                            break

                        attempted_this_run += 1
                        batch_progress += 1
                        try:
                            self._process_game(
                                layout=layout,
                                state=state,
                                state_path=state_path,
                                kalshi=kalshi,
                                mlb=mlb,
                                catalog_game=catalog_game,
                                mlb_games=mlb_games,
                                cutoff=cutoff,
                            )
                            completed_this_run += 1
                        except Exception as exc:
                            failed_this_run += 1
                            self._mark_game_failed(
                                state,
                                state_path,
                                catalog_game.event_ticker,
                                exc,
                            )

                        if batch_progress >= int(configuration["batch_size"]):
                            self._publish_checkpoint(layout, state)
                            batches_written += 1
                            batch_progress = 0
        except Exception as exc:
            self._record_job_error(state, state_path, exc)

        try:
            manifest = self._publish_checkpoint(layout, state, final=True)
            batches_written += 1
        except Exception as exc:
            self._record_job_error(state, state_path, exc)
            manifest = self._write_failure_manifest(layout, state)

        return {
            **manifest,
            "manifest": str((layout.run_dir / "manifest.json").resolve()),
            "resumed": resumed,
            "attempted_this_run": attempted_this_run,
            "completed_this_run": completed_this_run,
            "failed_this_run": failed_this_run,
            "batches_written": batches_written,
        }

    def _clients(self, raw: RawStore) -> _ClientContext:
        return _ClientContext(self.settings, raw)

    def _load_or_discover_catalog(
        self,
        *,
        layout: RunLayout,
        state: dict[str, Any],
        state_path: Path,
        kalshi: KalshiClient,
        configuration: dict[str, Any],
    ) -> list[KalshiGame]:
        catalog_path = layout.run_dir / "catalog.json"
        if catalog_path.is_file():
            return _catalog_games(_read_json(catalog_path))

        state["status"] = "discovering"
        state["discovery"] = {
            "status": "running",
            "started_at_utc": utc_iso(datetime.now(UTC)),
        }
        _write_state(state_path, state)
        games, metadata = kalshi.discover_games_in_range(
            start_date=date.fromisoformat(str(configuration["start_date"])),
            end_date=date.fromisoformat(str(configuration["end_date"])),
            max_games=int(configuration["max_games"]),
            max_pages=self.settings.max_discovery_pages,
        )
        truncated = list(metadata.get("truncated_sources", []))
        if truncated and len(games) < int(configuration["max_games"]):
            raise RuntimeError(
                "market discovery hit max_discovery_pages before reaching the "
                f"requested game count; truncated sources: {', '.join(truncated)}"
            )

        write_manifest(
            catalog_path,
            {
                "created_at_utc": utc_iso(datetime.now(UTC)),
                "configuration": configuration,
                "discovery": metadata,
                "games": [
                    {
                        "event_ticker": game.event_ticker,
                        "source": game.source,
                        "markets": list(game.markets),
                    }
                    for game in games
                ],
            },
        )
        state["discovery"] = {
            "status": "completed",
            "completed_at_utc": utc_iso(datetime.now(UTC)),
            "selected_games": len(games),
            "metadata": metadata,
            "catalog": str(catalog_path.resolve()),
        }
        state["status"] = "running"
        _write_state(state_path, state)
        return games

    def _initialize_game_checkpoints(
        self, state: dict[str, Any], games: list[KalshiGame]
    ) -> None:
        checkpoints = state["games"]
        for position, game in enumerate(games, start=1):
            checkpoints.setdefault(
                game.event_ticker,
                {
                    "position": position,
                    "status": "pending",
                    "attempts": 0,
                    "source_at_discovery": game.source,
                    "game_date": game.game_date.isoformat() if game.game_date else None,
                },
            )

    def _load_or_fetch_schedules(
        self,
        *,
        layout: RunLayout,
        state: dict[str, Any],
        state_path: Path,
        mlb: MlbClient,
        configuration: dict[str, Any],
    ) -> list[MlbGame]:
        start = date.fromisoformat(str(configuration["start_date"])) - timedelta(
            days=self.settings.schedule_buffer_days
        )
        end = date.fromisoformat(str(configuration["end_date"])) + timedelta(
            days=self.settings.schedule_buffer_days
        )
        schedule_rows: list[dict[str, Any]] = []
        for chunk_start, chunk_end in _date_chunks(start, end, SCHEDULE_CHUNK_DAYS):
            key = f"{chunk_start:%Y%m%d}_{chunk_end:%Y%m%d}"
            chunk_dir = layout.run_dir / "chunks" / "schedule" / key
            chunk_path = chunk_dir / "mlb_schedule.parquet"
            checkpoint = state["schedule_chunks"].setdefault(
                key,
                {
                    "start_date": chunk_start.isoformat(),
                    "end_date": chunk_end.isoformat(),
                    "status": "pending",
                    "attempts": 0,
                },
            )
            if checkpoint.get("status") != "completed" or not chunk_path.is_file():
                checkpoint["status"] = "running"
                checkpoint["attempts"] = int(checkpoint.get("attempts", 0)) + 1
                checkpoint["last_error"] = None
                _write_state(state_path, state)
                try:
                    payload = mlb.get_schedule(
                        start_date=chunk_start,
                        end_date=chunk_end,
                        raw_section=f"backfill_schedule_{key}",
                    )
                    rows = [
                        _mlb_game_row(game)
                        for game in normalize_mlb_schedule(payload)
                    ]
                    NormalizedStore(chunk_dir).write(
                        "mlb_schedule", rows, MLB_GAME_SCHEMA
                    )
                except Exception as exc:
                    checkpoint["status"] = "failed"
                    checkpoint["last_error"] = _error_payload(exc)
                    _write_state(state_path, state)
                    raise
                checkpoint.update(
                    {
                        "status": "completed",
                        "rows": len(rows),
                        "path": str(chunk_path.resolve()),
                        "completed_at_utc": utc_iso(datetime.now(UTC)),
                    }
                )
                _write_state(state_path, state)
            schedule_rows.extend(_read_parquet(chunk_path))

        by_game_pk = {
            int(row["game_pk"]): _mlb_game_from_row(row)
            for row in schedule_rows
            if row.get("game_pk") is not None
        }
        return sorted(
            by_game_pk.values(),
            key=lambda game: (game.scheduled_start_utc, game.game_pk),
        )

    def _process_game(
        self,
        *,
        layout: RunLayout,
        state: dict[str, Any],
        state_path: Path,
        kalshi: KalshiClient,
        mlb: MlbClient,
        catalog_game: KalshiGame,
        mlb_games: list[MlbGame],
        cutoff: dict[str, Any],
    ) -> None:
        event_ticker = catalog_game.event_ticker
        checkpoint = state["games"][event_ticker]
        checkpoint["status"] = "running"
        checkpoint["attempts"] = int(checkpoint.get("attempts", 0)) + 1
        checkpoint["started_at_utc"] = utc_iso(datetime.now(UTC))
        checkpoint["last_error"] = None
        _write_state(state_path, state)

        market_cutoff = parse_utc(str(cutoff["market_settled_ts"]))
        trade_cutoff = parse_utc(str(cutoff["trades_created_ts"]))
        event_source = _market_source(catalog_game.markets[0], market_cutoff)
        game = replace(catalog_game, source=event_source)
        match_result = match_game(
            game,
            mlb_games,
            tolerance=timedelta(minutes=self.settings.match_tolerance_minutes),
        )

        market_rows: list[dict[str, Any]] = []
        candle_rows: list[dict[str, Any]] = []
        trade_rows: list[dict[str, Any]] = []
        pagination: dict[str, Any] = {}

        for market in game.markets:
            ticker = str(market.get("ticker", ""))
            candle_source = _market_source(market, market_cutoff)
            row = _market_row(game, market)
            row["source"] = candle_source
            market_rows.append(row)
            if not isinstance(match_result, MatchedGame):
                continue

            window = market_window(market)
            if not ticker or window is None:
                raise ValueError("market has no ticker or valid open/settlement window")
            start, end = window

            candle_payload = kalshi.get_candlesticks(
                market=market,
                source=candle_source,
                start=start,
                end=end,
                raw_section=f"backfill_{event_ticker}",
            )
            candle_rows.extend(
                normalize_candlesticks(
                    event_ticker=event_ticker,
                    market_ticker=ticker,
                    source=candle_source,
                    payload=candle_payload,
                )
            )

            for trade_source, segment_start, segment_end in _trade_segments(
                start, end, trade_cutoff
            ):
                trades, metadata = kalshi.get_trades(
                    ticker=ticker,
                    source=trade_source,
                    start=segment_start,
                    end=segment_end,
                    max_pages=self.settings.max_trade_pages,
                    raw_section=f"backfill_{event_ticker}",
                )
                pagination[f"{trade_source}:{ticker}"] = metadata
                if metadata.get("truncated"):
                    raise RuntimeError(
                        f"trade pagination truncated for {ticker} ({trade_source})"
                    )
                trade_rows.extend(
                    normalize_trades(
                        event_ticker=event_ticker,
                        source=trade_source,
                        payloads=trades,
                    )
                )

        trade_rows = _deduplicate(trade_rows, ("trade_id",))
        match_rows: list[dict[str, Any]] = []
        rejection_rows: list[dict[str, Any]] = []
        play_rows: list[dict[str, Any]] = []
        if isinstance(match_result, MatchedGame):
            match_rows.append(_match_row(match_result))
            play_payload = mlb.get_play_by_play(match_result.mlb.game_pk)
            play_rows.extend(normalize_plays(match_result.mlb.game_pk, play_payload))
            outcome = "matched"
        else:
            rejection_rows.append(_rejection_row(match_result))
            outcome = f"rejected:{match_result.reason_code}"

        chunk_dir = _game_chunk_dir(layout, event_ticker)
        rows_by_dataset = {
            "markets": market_rows,
            "candlesticks": candle_rows,
            "trades": trade_rows,
            "mlb_plays": play_rows,
            "matches": match_rows,
            "rejections": rejection_rows,
        }
        store = NormalizedStore(chunk_dir)
        for dataset, rows in rows_by_dataset.items():
            file_name, schema = _DATASETS[dataset]
            store.write(file_name, rows, schema)

        checkpoint.update(
            {
                "status": "completed",
                "completed_at_utc": utc_iso(datetime.now(UTC)),
                "source_at_fetch": event_source,
                "outcome": outcome,
                "rows": {name: len(rows) for name, rows in rows_by_dataset.items()},
                "pagination": pagination,
                "chunk_dir": str(chunk_dir.resolve()),
            }
        )
        _write_state(state_path, state)
        self._log.info(
            "backfill_game_completed",
            event_ticker=event_ticker,
            outcome=outcome,
            position=checkpoint["position"],
        )

    def _mark_game_failed(
        self,
        state: dict[str, Any],
        state_path: Path,
        event_ticker: str,
        exc: Exception,
    ) -> None:
        checkpoint = state["games"][event_ticker]
        checkpoint["status"] = "failed"
        checkpoint["failed_at_utc"] = utc_iso(datetime.now(UTC))
        checkpoint["last_error"] = _error_payload(exc)
        _write_state(state_path, state)
        self._log.error(
            "backfill_game_failed",
            event_ticker=event_ticker,
            **checkpoint["last_error"],
        )

    def _record_job_error(
        self, state: dict[str, Any], state_path: Path, exc: Exception
    ) -> None:
        error = {
            **_error_payload(exc),
            "at_utc": utc_iso(datetime.now(UTC)),
        }
        state["last_error"] = error
        state["error_history"].append(error)
        state["status"] = "needs_retry"
        _write_state(state_path, state)
        self._log.error("backfill_job_failed", **error)

    def _publish_checkpoint(
        self,
        layout: RunLayout,
        state: dict[str, Any],
        *,
        final: bool = False,
    ) -> dict[str, Any]:
        paths, counts, rejection_counts = self._consolidate(layout, state)
        total = len(state["games"])
        completed = sum(
            checkpoint.get("status") == "completed"
            for checkpoint in state["games"].values()
        )
        failed = sum(
            checkpoint.get("status") == "failed"
            for checkpoint in state["games"].values()
        )
        if state.get("last_error") is not None or failed:
            status = "needs_retry"
        elif total == 0 and state.get("discovery", {}).get("status") == "completed":
            status = "empty"
        elif total > 0 and completed == total:
            status = "completed"
        else:
            status = "partial" if final else "running"
        state["status"] = status
        state["updated_at_utc"] = utc_iso(datetime.now(UTC))
        state["progress"] = {
            "total_games": total,
            "completed_games": completed,
            "failed_games": failed,
            "pending_games": total - completed - failed,
        }
        _write_state(layout.run_dir / "state.json", state)
        return self._write_manifest(
            layout,
            state,
            paths=paths,
            counts=counts,
            rejection_counts=rejection_counts,
        )

    def _consolidate(
        self, layout: RunLayout, state: dict[str, Any]
    ) -> tuple[dict[str, Path], dict[str, int], dict[str, int]]:
        rows_by_dataset: dict[str, list[dict[str, Any]]] = {
            dataset: [] for dataset in _DATASETS
        }
        for event_ticker, checkpoint in state["games"].items():
            if checkpoint.get("status") != "completed":
                continue
            chunk_dir = _game_chunk_dir(layout, event_ticker)
            missing = [
                file_name
                for file_name, _ in _DATASETS.values()
                if not (chunk_dir / f"{file_name}.parquet").is_file()
            ]
            if missing:
                checkpoint["status"] = "failed"
                checkpoint["last_error"] = {
                    "error_type": "MissingCheckpointFile",
                    "error": f"missing chunk files: {', '.join(missing)}",
                }
                continue
            for dataset, (file_name, _) in _DATASETS.items():
                rows_by_dataset[dataset].extend(
                    _read_parquet(chunk_dir / f"{file_name}.parquet")
                )

        rows_by_dataset["markets"] = _deduplicate(
            rows_by_dataset["markets"], ("ticker",)
        )
        rows_by_dataset["candlesticks"] = _deduplicate(
            rows_by_dataset["candlesticks"],
            ("ticker", "end_period_time_utc"),
        )
        rows_by_dataset["trades"] = _deduplicate(
            rows_by_dataset["trades"], ("trade_id",)
        )
        rows_by_dataset["mlb_plays"] = _deduplicate(
            rows_by_dataset["mlb_plays"], ("game_pk", "at_bat_index")
        )
        rows_by_dataset["matches"] = _deduplicate(
            rows_by_dataset["matches"], ("event_ticker",)
        )
        rows_by_dataset["rejections"] = _deduplicate(
            rows_by_dataset["rejections"], ("event_ticker",)
        )

        schedule_rows: list[dict[str, Any]] = []
        for checkpoint in state["schedule_chunks"].values():
            path_value = checkpoint.get("path")
            if checkpoint.get("status") == "completed" and path_value:
                path = Path(str(path_value))
                if path.is_file():
                    schedule_rows.extend(_read_parquet(path))
        schedule_rows = _deduplicate(schedule_rows, ("game_pk",))

        normalized = NormalizedStore(layout.normalized_dir)
        paths: dict[str, Path] = {}
        for dataset, rows in rows_by_dataset.items():
            file_name, schema = _DATASETS[dataset]
            paths[dataset] = normalized.write(file_name, rows, schema)
        paths["mlb_games"] = normalized.write(
            "mlb_schedule", schedule_rows, MLB_GAME_SCHEMA
        )

        rejection_payloads = []
        rejection_counts: dict[str, int] = {}
        for row in rows_by_dataset["rejections"]:
            reason_code = str(row.get("reason_code", ""))
            rejection_counts[reason_code] = rejection_counts.get(reason_code, 0) + 1
            rejection_payloads.append(
                {
                    "event_ticker": row.get("event_ticker"),
                    "reason_code": reason_code,
                    "reason": row.get("reason"),
                    "market_start_utc": row.get("market_start_utc"),
                    "market_teams": json.loads(
                        str(row.get("market_teams_json") or "[]")
                    ),
                    "details": json.loads(str(row.get("details_json") or "{}")),
                }
            )
        rejection_path = layout.run_dir / "rejections.json"
        write_manifest(rejection_path, {"rejections": rejection_payloads})
        paths["rejections_json"] = rejection_path

        counts = {
            "kalshi_games_selected": len(state["games"]),
            "kalshi_markets": len(rows_by_dataset["markets"]),
            "candlesticks_1m": len(rows_by_dataset["candlesticks"]),
            "trades": len(rows_by_dataset["trades"]),
            "mlb_schedule_games": len(schedule_rows),
            "matched_games": len(rows_by_dataset["matches"]),
            "rejected_games": len(rows_by_dataset["rejections"]),
            "plays": len(rows_by_dataset["mlb_plays"]),
        }
        return paths, counts, rejection_counts

    def _write_manifest(
        self,
        layout: RunLayout,
        state: dict[str, Any],
        *,
        paths: dict[str, Path],
        counts: dict[str, int],
        rejection_counts: dict[str, int],
    ) -> dict[str, Any]:
        progress = state.get("progress", {})
        failed_resources = [
            {
                "event_ticker": event_ticker,
                **cast(dict[str, Any], checkpoint.get("last_error", {})),
            }
            for event_ticker, checkpoint in state["games"].items()
            if checkpoint.get("status") == "failed"
        ]
        manifest = {
            "run_id": layout.run_id,
            "run_type": "backfill",
            "status": state["status"],
            "created_at_utc": state["created_at_utc"],
            "updated_at_utc": state["updated_at_utc"],
            "configuration": state["configuration"],
            "counts": {
                **counts,
                "completed_games": int(progress.get("completed_games", 0)),
                "failed_games": int(progress.get("failed_games", 0)),
                "pending_games": int(progress.get("pending_games", 0)),
                "resource_errors": len(failed_resources)
                + int(state.get("last_error") is not None),
            },
            "rejection_reason_counts": rejection_counts,
            "resource_errors": failed_resources,
            "last_error": state.get("last_error"),
            "discovery": state["discovery"],
            "last_cutoff": state.get("last_cutoff"),
            "checkpoint": {
                "state": str((layout.run_dir / "state.json").resolve()),
                "catalog": str((layout.run_dir / "catalog.json").resolve()),
                "chunks": str((layout.run_dir / "chunks").resolve()),
            },
            "raw_dir": str(layout.raw_dir.resolve()),
            "normalized_files": {
                key: str(path.resolve())
                for key, path in paths.items()
                if key != "rejections_json"
            },
            "rejections_json": str(paths["rejections_json"].resolve()),
        }
        write_manifest(layout.run_dir / "manifest.json", manifest)
        return manifest

    def _write_failure_manifest(
        self, layout: RunLayout, state: dict[str, Any]
    ) -> dict[str, Any]:
        state["status"] = "needs_retry"
        state["updated_at_utc"] = utc_iso(datetime.now(UTC))
        _write_state(layout.run_dir / "state.json", state)
        manifest = {
            "run_id": layout.run_id,
            "run_type": "backfill",
            "status": "needs_retry",
            "created_at_utc": state["created_at_utc"],
            "updated_at_utc": state["updated_at_utc"],
            "configuration": state["configuration"],
            "counts": {
                "kalshi_games_selected": len(state["games"]),
                "completed_games": sum(
                    checkpoint.get("status") == "completed"
                    for checkpoint in state["games"].values()
                ),
                "failed_games": sum(
                    checkpoint.get("status") == "failed"
                    for checkpoint in state["games"].values()
                ),
                "resource_errors": 1,
            },
            "last_error": state.get("last_error"),
            "checkpoint": {
                "state": str((layout.run_dir / "state.json").resolve()),
                "catalog": str((layout.run_dir / "catalog.json").resolve()),
                "chunks": str((layout.run_dir / "chunks").resolve()),
            },
            "raw_dir": str(layout.raw_dir.resolve()),
            "normalized_files": {},
        }
        write_manifest(layout.run_dir / "manifest.json", manifest)
        return manifest


def _new_configuration(
    *,
    start_date: date | None,
    end_date: date | None,
    max_games: int | None,
    batch_size: int | None,
) -> dict[str, Any]:
    if start_date is None or end_date is None:
        raise ValueError("start_date and end_date are required for a new backfill job")
    if end_date < start_date:
        raise ValueError("end_date cannot precede start_date")
    requested_games = DEFAULT_MAX_GAMES if max_games is None else max_games
    requested_batch = DEFAULT_BATCH_SIZE if batch_size is None else batch_size
    if not 1 <= requested_games <= 10_000:
        raise ValueError("max_games must be between 1 and 10000")
    if not 1 <= requested_batch <= 500:
        raise ValueError("batch_size must be between 1 and 500")
    return {
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "max_games": requested_games,
        "batch_size": requested_batch,
        "schedule_chunk_days": SCHEDULE_CHUNK_DAYS,
    }


def _resume_configuration(
    state: dict[str, Any],
    *,
    start_date: date | None,
    end_date: date | None,
    max_games: int | None,
    batch_size: int | None,
) -> dict[str, Any]:
    configuration = cast(dict[str, Any], state["configuration"])
    supplied = {
        "start_date": start_date.isoformat() if start_date else None,
        "end_date": end_date.isoformat() if end_date else None,
        "max_games": max_games,
        "batch_size": batch_size,
    }
    conflicts = [
        key
        for key, value in supplied.items()
        if value is not None and value != configuration.get(key)
    ]
    if conflicts:
        raise ValueError(
            "resume arguments conflict with the existing checkpoint: "
            + ", ".join(conflicts)
        )
    return configuration


def _catalog_games(payload: dict[str, Any]) -> list[KalshiGame]:
    games: list[KalshiGame] = []
    for item in payload.get("games", []):
        if not isinstance(item, dict):
            continue
        event_ticker = item.get("event_ticker")
        source = item.get("source")
        markets = item.get("markets")
        if (
            isinstance(event_ticker, str)
            and source in {"current", "historical"}
            and isinstance(markets, list)
        ):
            games.append(
                build_kalshi_game(
                    event_ticker,
                    cast(MarketSource, source),
                    [market for market in markets if isinstance(market, dict)],
                )
            )
    return games


def _market_source(
    market: dict[str, Any], market_cutoff: datetime
) -> MarketSource:
    settled = optional_utc(market.get("settlement_ts"))
    return "historical" if settled is not None and settled < market_cutoff else "current"


def _trade_segments(
    start: datetime, end: datetime, cutoff: datetime
) -> list[tuple[MarketSource, datetime, datetime]]:
    segments: list[tuple[MarketSource, datetime, datetime]] = []
    if start <= cutoff:
        segments.append(("historical", start, min(end, cutoff)))
    if end >= cutoff:
        segments.append(("current", max(start, cutoff), end))
    return [
        (source, segment_start, segment_end)
        for source, segment_start, segment_end in segments
        if segment_end >= segment_start
    ]


def _date_chunks(
    start: date, end: date, chunk_days: int
) -> list[tuple[date, date]]:
    chunks: list[tuple[date, date]] = []
    cursor = start
    while cursor <= end:
        chunk_end = min(end, cursor + timedelta(days=chunk_days - 1))
        chunks.append((cursor, chunk_end))
        cursor = chunk_end + timedelta(days=1)
    return chunks


def _game_chunk_dir(layout: RunLayout, event_ticker: str) -> Path:
    safe_ticker = _SAFE_SEGMENT.sub("_", event_ticker)
    return layout.run_dir / "chunks" / "games" / safe_ticker


def _mlb_game_from_row(row: dict[str, Any]) -> MlbGame:
    return MlbGame(
        game_pk=int(row["game_pk"]),
        official_date=cast(date, row["official_date"]),
        scheduled_start_utc=parse_utc(cast(datetime, row["scheduled_start_utc"])),
        away_team=str(row["away_team"]),
        home_team=str(row["home_team"]),
        away_name=str(row["away_name"]),
        home_name=str(row["home_name"]),
        away_score=cast(int | None, row.get("away_score")),
        home_score=cast(int | None, row.get("home_score")),
        final=bool(row["final"]),
        detailed_state=str(row["detailed_state"]),
        doubleheader=bool(row["doubleheader"]),
        game_number=cast(int | None, row.get("game_number")),
        raw={},
    )


def _deduplicate(
    rows: list[dict[str, Any]], keys: tuple[str, ...]
) -> list[dict[str, Any]]:
    unique = {
        tuple(row.get(key) for key in keys): row
        for row in rows
    }
    return [
        unique[key]
        for key in sorted(unique, key=lambda value: tuple(str(item) for item in value))
    ]


def _read_parquet(path: Path) -> list[dict[str, Any]]:
    return cast(list[dict[str, Any]], pq.read_table(path).to_pylist())


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected a JSON object in {path}")
    return payload


def _write_state(path: Path, state: dict[str, Any]) -> None:
    state["updated_at_utc"] = utc_iso(datetime.now(UTC))
    write_manifest(path, state)


def _error_payload(exc: Exception) -> dict[str, str]:
    return {"error_type": type(exc).__name__, "error": str(exc)}


def _validate_job_id(job_id: str) -> None:
    if not _JOB_ID.fullmatch(job_id):
        raise ValueError(
            "job_id must be 1-64 characters using letters, numbers, '.', '_' or '-'"
        )
