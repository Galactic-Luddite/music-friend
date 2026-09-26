"""Issue #49: per-source pacing profiles and the suspend-aware refresh deadline clock."""

from __future__ import annotations

import random
import time
from datetime import datetime, timezone

import pytest

from music_friend.domain import SourceLimitObservation, SourceLimitState
from music_friend.errors import RateLimitedError
from music_friend.tools import refresh
from music_friend.tools.release_discovery import _SourceCallStopped

NOW = datetime(2026, 9, 2, 12, tzinfo=timezone.utc)


class _Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += seconds


def _paced(
    name: str, clock: _Clock, saved: SourceLimitObservation | None = None
) -> refresh._PacedSource:
    return refresh._PacedSource(
        object(),  # type: ignore[arg-type]
        source_name=name,
        started_at=0.0,
        checked_at=NOW,
        monotonic=clock,
        sleeper=clock.sleep,
        saved_limit=saved,
        rng=random.Random(0),
    )


def test_musicbrainz_keeps_one_request_per_second_after_a_limit_and_a_low_saved_budget() -> None:
    clock = _Clock()
    saved = SourceLimitObservation(
        "musicbrainz", SourceLimitState.AVAILABLE, NOW, None, False, 0, 1
    )
    paced = _paced("musicbrainz", clock, saved)
    responses: list[object] = [RateLimitedError(retry_after_seconds=1), "ok"] + ["ok"] * 9

    def call() -> object:
        value = responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    started: list[float] = []

    def timed() -> object:
        started.append(clock.value)
        return call()

    for _ in range(10):
        assert paced._request(timed) == "ok"

    gaps = [later - earlier for earlier, later in zip(started[1:], started[2:])]
    assert all(gap == pytest.approx(1.0) for gap in gaps)
    assert paced.window_calls == 1
    assert paced.profile.window_seconds == 1.0


def test_spotify_keeps_its_adaptive_thirty_second_budget() -> None:
    clock = _Clock()
    paced = _paced("spotify", clock)
    assert paced.profile.adaptive
    assert paced.profile.window_seconds == 30.0
    paced._on_limited()
    assert paced.window_calls == 4


def test_a_deadline_stop_records_its_reason() -> None:
    clock = _Clock()
    paced = _paced("musicbrainz", clock)
    clock.value = 600.0
    with pytest.raises(_SourceCallStopped):
        paced._request(lambda: "never")
    assert paced.stop_reason == "deadline"


@pytest.mark.parametrize(
    ("platform", "clock_id"),
    (("darwin", "CLOCK_MONOTONIC"), ("linux", "CLOCK_BOOTTIME")),
)
def test_deadline_clock_counts_time_the_host_spends_suspended(
    monkeypatch: pytest.MonkeyPatch, platform: str, clock_id: str
) -> None:
    requested: list[int] = []
    expected = getattr(time, clock_id, object())
    monkeypatch.setattr(refresh.sys, "platform", platform)
    monkeypatch.setattr(time, clock_id, expected, raising=False)
    monkeypatch.setattr(
        time, "clock_gettime", lambda clock: requested.append(clock) or 42.0, raising=False
    )
    assert refresh._deadline_clock() == 42.0
    assert requested == [expected]


def test_deadline_clock_uses_monotonic_elsewhere(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(refresh.sys, "platform", "win32")
    monkeypatch.setattr(time, "monotonic", lambda: 7.0)
    assert refresh._deadline_clock() == 7.0
