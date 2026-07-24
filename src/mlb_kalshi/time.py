from __future__ import annotations

from datetime import UTC, datetime


def parse_utc(value: str | datetime) -> datetime:
    """Parse an aware timestamp and normalize it to UTC."""

    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"timestamp must include a UTC offset: {value!r}")
    return parsed.astimezone(UTC)


def optional_utc(value: object) -> datetime | None:
    if not isinstance(value, (str, datetime)) or value == "":
        return None
    return parse_utc(value)


def unix_seconds(value: datetime) -> int:
    return int(parse_utc(value).timestamp())


def utc_iso(value: datetime) -> str:
    return parse_utc(value).isoformat().replace("+00:00", "Z")
