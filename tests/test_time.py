from datetime import UTC, datetime

import pytest

from mlb_kalshi.time import parse_utc, unix_seconds, utc_iso


def test_offset_timestamp_is_normalized_to_utc() -> None:
    value = parse_utc("2025-07-04T19:05:00-04:00")
    assert value == datetime(2025, 7, 4, 23, 5, tzinfo=UTC)
    assert utc_iso(value) == "2025-07-04T23:05:00Z"


def test_unix_timestamp_uses_utc_instant() -> None:
    assert unix_seconds(parse_utc("1970-01-01T01:00:00+01:00")) == 0


def test_naive_timestamp_is_rejected() -> None:
    with pytest.raises(ValueError, match="UTC offset"):
        parse_utc("2025-07-04T19:05:00")
