"""Provider-neutral listening-history observations and precise UTC timestamps."""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

_TIMESTAMP = re.compile(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d+))?(Z|[+-]\d{2}:\d{2})\Z")


@dataclass(frozen=True, slots=True)
class RecentPlay:
    track_uri: str
    track_name: str
    primary_artist_name: str
    album_name: str
    played_at: str
    context_uri: str | None = None

    def __post_init__(self) -> None:
        for name in ("track_uri", "track_name", "primary_artist_name", "album_name", "played_at"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value or len(value) > 4096:
                raise ValueError(f"{name} must be bounded text")
        if self.context_uri is not None and (
            not isinstance(self.context_uri, str)
            or not self.context_uri
            or len(self.context_uri) > 4096
        ):
            raise ValueError("context_uri must be bounded text or None")


def canonical_history_timestamp(value: str) -> tuple[int, str, str]:
    match = _TIMESTAMP.fullmatch(value)
    if match is None:
        raise ValueError("played_at must be an ISO-8601 timestamp with an offset")
    base, fraction, offset = match.groups()
    local = datetime.strptime(base, "%Y-%m-%dT%H:%M:%S")
    if offset != "Z" and (int(offset[1:3]) > 23 or int(offset[4:6]) > 59):
        raise ValueError("played_at has an invalid UTC offset")
    offset_seconds = (
        0
        if offset == "Z"
        else (1 if offset[0] == "+" else -1) * (int(offset[1:3]) * 3600 + int(offset[4:6]) * 60)
    )
    try:
        utc = local - timedelta(seconds=offset_seconds)
    except OverflowError as error:
        raise ValueError("played_at is outside the supported timestamp range") from error
    seconds = calendar.timegm(utc.timetuple())
    digits = (fraction or "").rstrip("0")
    canonical = datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    return seconds, digits, canonical + (("." + digits) if digits else "") + "Z"


def history_request_after_ms(played_at: str) -> int:
    seconds, digits, _ = canonical_history_timestamp(played_at)
    milliseconds = int((digits + "000")[:3]) if digits else 0
    return max(0, seconds * 1000 + milliseconds - 1)


__all__ = ["RecentPlay", "canonical_history_timestamp", "history_request_after_ms"]
