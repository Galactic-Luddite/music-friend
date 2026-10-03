import random
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from music_friend.domain import SourceLimitObservation, SourceLimitState
from music_friend.errors import InvalidSourceResponseError, QuotaExhaustedError, RateLimitedError
from music_friend.providers import Capability, Page, ProviderCapabilities, RecentPlay
from music_friend.providers.spotify.config import SpotifySettings
from music_friend.providers.spotify.source import SpotifySource
from music_friend.store import Catalog
from music_friend.tools.application import MusicFriendApplication
from music_friend.tools.refresh import _PacedSource, sync_recent_history


@pytest.fixture
def catalog(tmp_path: Path) -> Iterator[Catalog]:
    opened = Catalog.open(tmp_path / "private" / "catalog.sqlite3")
    try:
        yield opened
    finally:
        opened.close()


class Source:
    def __init__(self, pages: list[Page[RecentPlay]], granted: bool = True) -> None:
        self.pages = pages
        self.calls: list[tuple[int | None, str | None]] = []
        self.granted = granted

    def capabilities(self) -> ProviderCapabilities:
        granted = frozenset({Capability.RECENT_PLAYS}) if self.granted else frozenset()
        return ProviderCapabilities(frozenset({Capability.RECENT_PLAYS}), granted)

    def recent_plays(
        self, after_ms: int | None = None, cursor: str | None = None
    ) -> Page[RecentPlay]:
        self.calls.append((after_ms, cursor))
        return self.pages.pop(0)


def play(at: str, uri: str = "spotify:track:a") -> RecentPlay:
    return RecentPlay(uri, "Track A", "Artist A", "Album A", at)


def test_sync_is_bounded_and_freshness_skips_without_calls(catalog: Catalog) -> None:
    app = MusicFriendApplication(catalog)
    source = Source([Page((play("2030-01-01T00:00:00.123456789Z"),), "next"), Page(())])
    at = datetime(2030, 1, 2, tzinfo=timezone.utc)
    result = sync_recent_history(app, "spotify", source, checked_at=at)
    assert (result.outcome, result.attempts, result.pages, result.observations) == (
        "first_snapshot",
        2,
        2,
        1,
    )
    fresh = sync_recent_history(app, "spotify", source, checked_at=at + timedelta(hours=1))
    assert fresh.outcome == "skipped_fresh"
    assert len(source.calls) == 2


def test_sync_missing_permission_makes_zero_calls(catalog: Catalog) -> None:
    source = Source([], granted=False)
    result = sync_recent_history(
        MusicFriendApplication(catalog),
        "spotify",
        source,
        checked_at=datetime(2030, 1, 2, tzinfo=timezone.utc),
    )
    assert result.outcome == "permission_required"
    assert source.calls == []


@pytest.mark.parametrize(
    ("state", "retry_at", "expected"),
    (
        (SourceLimitState.QUOTA_EXHAUSTED, None, "quota_exhausted"),
        (
            SourceLimitState.COOLING_DOWN,
            datetime(2030, 1, 2, 1, tzinfo=timezone.utc),
            "cooling_down",
        ),
    ),
)
def test_saved_history_limits_skip_without_source_calls(
    catalog: Catalog,
    state: SourceLimitState,
    retry_at: datetime | None,
    expected: str,
) -> None:
    checked_at = datetime(2030, 1, 2, tzinfo=timezone.utc)
    app = MusicFriendApplication(catalog)
    app.put_source_limit(SourceLimitObservation("spotify", state, checked_at, retry_at, False, 1))
    source = Source([])

    result = sync_recent_history(app, "spotify", source, checked_at=checked_at)

    assert result.outcome == expected
    assert source.calls == []


def test_partial_advances_boundary_without_success(catalog: Catalog) -> None:
    app = MusicFriendApplication(catalog)
    source = Source(
        [
            Page((play("2030-01-01T00:00:00.999999999Z"),), "again"),
            Page((play("2030-01-01T00:01:00Z", "spotify:track:b"),), "more"),
        ]
    )
    result = sync_recent_history(
        app, "spotify", source, checked_at=datetime(2030, 1, 2, tzinfo=timezone.utc)
    )
    assert result.outcome == "bounded_partial"
    state = app.get_recent_history_state("spotify")
    assert state is not None and state.last_successful_check_at is None and state.needs_repair
    later = Source([Page(())])
    sync_recent_history(app, "spotify", later, checked_at=datetime(2030, 1, 3, tzinfo=timezone.utc))
    assert later.calls[0][0] == 1893456059999


def test_stalled_cursor_retains_pages_and_requires_repair(catalog: Catalog) -> None:
    app = MusicFriendApplication(catalog)
    source = Source(
        [
            Page((play("2030-01-01T00:00:00Z"),), "same"),
            Page((play("2030-01-01T00:01:00Z", "spotify:track:b"),), "same"),
        ]
    )

    result = sync_recent_history(
        app, "spotify", source, checked_at=datetime(2030, 1, 2, tzinfo=timezone.utc)
    )

    assert (result.outcome, result.reason, result.observations) == (
        "bounded_partial",
        "stalled_cursor",
        2,
    )
    state = app.get_recent_history_state("spotify")
    assert state is not None and state.needs_repair


def test_oversized_page_records_invalid_response_without_observations(catalog: Catalog) -> None:
    source = Source(
        [Page(tuple(play("2030-01-01T00:00:00Z", f"spotify:track:{index}") for index in range(51)))]
    )
    app = MusicFriendApplication(catalog)

    result = sync_recent_history(
        app, "spotify", source, checked_at=datetime(2030, 1, 2, tzinfo=timezone.utc)
    )

    assert (result.outcome, result.reason, result.observations) == (
        "failed",
        "invalid_response",
        0,
    )
    assert result.attempts == 1
    assert app.recent_history_store().list_observations("spotify") == ()


def test_later_partial_preserves_previous_success_and_forces_repair(catalog: Catalog) -> None:
    app = MusicFriendApplication(catalog)
    at = datetime(2030, 1, 2, tzinfo=timezone.utc)
    sync_recent_history(app, "spotify", Source([Page(())]), checked_at=at)
    partial = Source(
        [
            Page((play("2030-01-02T01:00:00Z"),), "next"),
            Page((play("2030-01-02T02:00:00Z"),), "more"),
        ]
    )
    sync_recent_history(app, "spotify", partial, checked_at=at + timedelta(hours=21))
    state = app.get_recent_history_state("spotify")
    assert state is not None and state.last_successful_check_at == at and state.needs_repair


def test_interrupted_running_attempt_is_eligible_despite_recent_success(catalog: Catalog) -> None:
    app = MusicFriendApplication(catalog)
    at = datetime(2030, 1, 2, tzinfo=timezone.utc)
    sync_recent_history(app, "spotify", Source([Page(())]), checked_at=at)
    app.recent_history_store().begin_attempt(
        "spotify", at + timedelta(minutes=1), requested_after_ms=None
    )
    retry = Source([Page(())])
    result = sync_recent_history(app, "spotify", retry, checked_at=at + timedelta(minutes=2))
    assert result.outcome == "terminal_empty" and len(retry.calls) == 1


def test_paced_recent_history_does_not_retry_429() -> None:
    class Limited(Source):
        def recent_plays(
            self, after_ms: int | None = None, cursor: str | None = None
        ) -> Page[RecentPlay]:
            self.calls.append((after_ms, cursor))
            raise RateLimitedError("30")

    source = Limited([])
    paced = _PacedSource(
        source,
        source_name="spotify",
        started_at=0.0,
        checked_at=datetime(2030, 1, 1, tzinfo=timezone.utc),
        monotonic=lambda: 0.0,
        sleeper=lambda _delay: pytest.fail("history must not pause/retry"),
        saved_limit=None,
        rng=random.Random(1),
    )
    with pytest.raises(RateLimitedError):
        paced.recent_plays()
    assert len(source.calls) == 1
    assert paced.stopped and paced.current_observation().state is SourceLimitState.COOLING_DOWN


def test_paced_recent_history_checkpoints_quota_without_retry() -> None:
    class Exhausted(Source):
        def recent_plays(
            self, after_ms: int | None = None, cursor: str | None = None
        ) -> Page[RecentPlay]:
            self.calls.append((after_ms, cursor))
            raise QuotaExhaustedError()

    source = Exhausted([])
    paced = _PacedSource(
        source,
        source_name="spotify",
        started_at=0.0,
        checked_at=datetime(2030, 1, 1, tzinfo=timezone.utc),
        monotonic=lambda: 0.0,
        sleeper=lambda _delay: pytest.fail("history must not pause/retry"),
        saved_limit=None,
        rng=random.Random(1),
    )

    with pytest.raises(QuotaExhaustedError):
        paced.recent_plays()

    assert len(source.calls) == 1
    assert paced.stopped
    assert paced.current_observation().state is SourceLimitState.QUOTA_EXHAUSTED


def test_invalid_provider_timestamp_records_failed_repair_outcome(catalog: Catalog) -> None:
    class Tokens:
        def capabilities(self) -> ProviderCapabilities:
            return ProviderCapabilities(
                frozenset({Capability.RECENT_PLAYS}), frozenset({Capability.RECENT_PLAYS})
            )

        def _call_deadline(self) -> float:
            return 10.0

        def _execute(self, _operation, *, query, deadline):
            return {
                "items": [
                    {
                        "played_at": "2030-02-30T00:00:00Z",
                        "track": {
                            "uri": "spotify:track:synthetic",
                            "name": "Synthetic",
                            "artists": [{"name": "Synthetic artist"}],
                            "album": {"name": "Synthetic album"},
                        },
                    }
                ],
                "next": None,
                "cursors": {},
            }

    source = SpotifySource(
        settings=SpotifySettings(
            client_id="synthetic", redirect_uri="http://127.0.0.1:8888/callback"
        ),
        tokens=Tokens(),
        clock=lambda: datetime(2030, 1, 1, tzinfo=timezone.utc),
    )
    app = MusicFriendApplication(catalog)

    result = sync_recent_history(
        app, "spotify", source, checked_at=datetime(2030, 1, 2, tzinfo=timezone.utc)
    )

    assert (result.outcome, result.reason, result.observations) == (
        "failed",
        "invalid_response",
        0,
    )
    state = app.get_recent_history_state("spotify")
    assert state is not None and state.needs_repair


def test_spotify_before_continuation_reaches_terminal_success(catalog: Catalog) -> None:
    class Tokens:
        def __init__(self) -> None:
            self.queries: list[tuple[tuple[str, str], ...]] = []
            self.responses = iter(
                (
                    {
                        "items": [
                            {
                                "played_at": "2030-01-01T00:00:00.123456789Z",
                                "track": {
                                    "uri": "spotify:track:synthetic",
                                    "name": "Synthetic",
                                    "artists": [{"name": "Synthetic artist"}],
                                    "album": {"name": "Synthetic album"},
                                },
                            },
                            {
                                "played_at": "2030-01-01T00:00:00.123456789Z",
                                "track": {
                                    "uri": "spotify:track:synthetic",
                                    "name": "Synthetic",
                                    "artists": [{"name": "Synthetic artist"}],
                                    "album": {"name": "Synthetic album"},
                                },
                            },
                        ],
                        "next": (
                            "https://api.spotify.com/v1/me/player/recently-played"
                            "?before=1893455999000&limit=50"
                        ),
                        "cursors": {"after": "1893456000123", "before": "1893455999000"},
                    },
                    {"items": [], "next": None, "cursors": None},
                )
            )

        def capabilities(self) -> ProviderCapabilities:
            return ProviderCapabilities(
                frozenset({Capability.RECENT_PLAYS}), frozenset({Capability.RECENT_PLAYS})
            )

        def _call_deadline(self) -> float:
            return 10.0

        def _execute(self, _operation, *, query, deadline):
            self.queries.append(query)
            return next(self.responses)

    tokens = Tokens()
    source = SpotifySource(
        settings=SpotifySettings(
            client_id="synthetic", redirect_uri="http://127.0.0.1:8888/callback"
        ),
        tokens=tokens,
        clock=lambda: datetime(2030, 1, 1, tzinfo=timezone.utc),
    )
    app = MusicFriendApplication(catalog)

    result = sync_recent_history(
        app, "spotify", source, checked_at=datetime(2030, 1, 2, tzinfo=timezone.utc)
    )

    assert (result.outcome, result.attempts, result.pages, result.observations) == (
        "first_snapshot",
        2,
        2,
        1,
    )
    assert tokens.queries == [
        (("limit", "50"),),
        (("limit", "50"), ("before", "1893455999000")),
    ]
    state = app.get_recent_history_state("spotify")
    assert state is not None and not state.needs_repair
    assert len(app.recent_history_store().list_observations("spotify")) == 1

    fresh = sync_recent_history(
        app,
        "spotify",
        source,
        checked_at=datetime(2030, 1, 2, 1, tzinfo=timezone.utc),
    )
    assert fresh.outcome == "skipped_fresh"
    assert len(tokens.queries) == 2


def test_invalid_later_page_retains_earlier_committed_observations(catalog: Catalog) -> None:
    class InvalidSecondPage(Source):
        def recent_plays(
            self, after_ms: int | None = None, cursor: str | None = None
        ) -> Page[RecentPlay]:
            self.calls.append((after_ms, cursor))
            if len(self.calls) == 2:
                raise InvalidSourceResponseError()
            return self.pages.pop(0)

    app = MusicFriendApplication(catalog)
    source = InvalidSecondPage([Page((play("2030-01-01T00:00:00.123456789Z"),), "next")])

    result = sync_recent_history(
        app, "spotify", source, checked_at=datetime(2030, 1, 2, tzinfo=timezone.utc)
    )

    assert (result.outcome, result.reason, result.observations) == (
        "failed",
        "invalid_response",
        1,
    )
    assert len(app.recent_history_store().list_observations("spotify")) == 1
