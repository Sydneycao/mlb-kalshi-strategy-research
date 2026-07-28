from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pyarrow as pa  # type: ignore[import-untyped]
import pyarrow.parquet as pq  # type: ignore[import-untyped]

UTC_TIMESTAMP = pa.timestamp("us", tz="UTC")

MARKET_SCHEMA = pa.schema(
    [
        ("event_ticker", pa.string()),
        ("ticker", pa.string()),
        ("source", pa.string()),
        ("status", pa.string()),
        ("result", pa.string()),
        ("title", pa.string()),
        ("yes_team", pa.string()),
        ("no_team", pa.string()),
        ("open_time_utc", UTC_TIMESTAMP),
        ("close_time_utc", UTC_TIMESTAMP),
        ("occurrence_time_utc", UTC_TIMESTAMP),
        ("settlement_time_utc", UTC_TIMESTAMP),
        ("volume_fp", pa.string()),
        ("open_interest_fp", pa.string()),
    ]
)

CANDLE_SCHEMA = pa.schema(
    [
        ("event_ticker", pa.string()),
        ("ticker", pa.string()),
        ("source", pa.string()),
        ("end_period_time_utc", UTC_TIMESTAMP),
        ("volume_fp", pa.string()),
        ("open_interest_fp", pa.string()),
        *[
            (f"{quote}_{field}", pa.string())
            for quote in ("yes_bid", "yes_ask", "price")
            for field in (
                "open_dollars",
                "low_dollars",
                "high_dollars",
                "close_dollars",
                "mean_dollars",
                "previous_dollars",
                "min_dollars",
                "max_dollars",
            )
        ],
    ]
)

TRADE_SCHEMA = pa.schema(
    [
        ("trade_id", pa.string()),
        ("event_ticker", pa.string()),
        ("ticker", pa.string()),
        ("source", pa.string()),
        ("created_time_utc", UTC_TIMESTAMP),
        ("count_fp", pa.string()),
        ("yes_price_dollars", pa.string()),
        ("no_price_dollars", pa.string()),
        ("taker_side", pa.string()),
    ]
)

MLB_GAME_SCHEMA = pa.schema(
    [
        ("game_pk", pa.int64()),
        ("official_date", pa.date32()),
        ("scheduled_start_utc", UTC_TIMESTAMP),
        ("away_team", pa.string()),
        ("home_team", pa.string()),
        ("away_name", pa.string()),
        ("home_name", pa.string()),
        ("away_score", pa.int32()),
        ("home_score", pa.int32()),
        ("winner", pa.string()),
        ("final", pa.bool_()),
        ("detailed_state", pa.string()),
        ("doubleheader", pa.bool_()),
        ("game_number", pa.int32()),
    ]
)

PLAY_SCHEMA = pa.schema(
    [
        ("game_pk", pa.int64()),
        ("at_bat_index", pa.int32()),
        ("event", pa.string()),
        ("description", pa.string()),
        ("inning", pa.int32()),
        ("half_inning", pa.string()),
        ("start_time_utc", UTC_TIMESTAMP),
        ("end_time_utc", UTC_TIMESTAMP),
        ("away_score", pa.int32()),
        ("home_score", pa.int32()),
        ("is_complete", pa.bool_()),
    ]
)

MATCH_SCHEMA = pa.schema(
    [
        ("event_ticker", pa.string()),
        ("game_pk", pa.int64()),
        ("source", pa.string()),
        ("market_start_utc", UTC_TIMESTAMP),
        ("mlb_start_utc", UTC_TIMESTAMP),
        ("start_delta_seconds", pa.int64()),
        ("away_team", pa.string()),
        ("home_team", pa.string()),
        ("winner", pa.string()),
    ]
)

REJECTION_SCHEMA = pa.schema(
    [
        ("event_ticker", pa.string()),
        ("reason_code", pa.string()),
        ("reason", pa.string()),
        ("market_start_utc", UTC_TIMESTAMP),
        ("market_teams_json", pa.string()),
        ("details_json", pa.string()),
    ]
)

_SAFE_SEGMENT = re.compile(r"[^A-Za-z0-9_.=-]+")


def new_run_id(prefix: str) -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    return f"{prefix}_{stamp}"


@dataclass(frozen=True, slots=True)
class RunLayout:
    output_dir: Path
    run_id: str

    @property
    def raw_dir(self) -> Path:
        return self.output_dir / "raw" / self.run_id

    @property
    def normalized_dir(self) -> Path:
        return self.output_dir / "normalized" / self.run_id

    @property
    def run_dir(self) -> Path:
        return self.output_dir / "runs" / self.run_id

    def create(self) -> None:
        self.raw_dir.mkdir(parents=True, exist_ok=False)
        self.normalized_dir.mkdir(parents=True, exist_ok=False)
        self.run_dir.mkdir(parents=True, exist_ok=False)

    def create_or_resume(self) -> None:
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.normalized_dir.mkdir(parents=True, exist_ok=True)
        self.run_dir.mkdir(parents=True, exist_ok=True)


class RawStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def write_json(self, *segments: str, payload: dict[str, Any]) -> Path:
        if not segments:
            raise ValueError("at least one path segment is required")
        safe_segments = [_SAFE_SEGMENT.sub("_", segment) for segment in segments]
        path = self.root.joinpath(*safe_segments).with_suffix(".json")
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_json(path, payload)
        return path


class NormalizedStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def write(
        self, name: str, records: list[dict[str, Any]], schema: pa.Schema
    ) -> Path:
        path = self.root / f"{_SAFE_SEGMENT.sub('_', name)}.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        table = pa.Table.from_pylist(records, schema=schema)
        temp_path = path.with_name(f".{path.name}.tmp")
        pq.write_table(table, temp_path, compression="zstd")
        os.replace(temp_path, path)
        return path


def write_manifest(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_json(path, payload)


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    temp_path = path.with_name(f".{path.name}.tmp")
    with temp_path.open("w", encoding="utf-8") as handle:
        json.dump(
            payload,
            handle,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            default=_json_default,
        )
        handle.write("\n")
    os.replace(temp_path, path)


def _json_default(value: object) -> str:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"cannot JSON serialize {type(value).__name__}")
