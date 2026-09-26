"""Issue #49: refresh pacing, deadline, and honest status through the shipped entry points.

Every test drives ``music-friend refresh ...`` through ``cli.run_cli`` or the MCP
``refresh_music`` closure built by ``mcp_stdio.run_catalog_stdio_session`` -- never
``refresh_once`` directly -- on a fake clock that both the refresh deadline and every
provider's local pacing read, with all local state under ``tmp_path``.

MusicBrainz response bodies are the shapes the provider code documents as verified
live (``/ws/2/url`` batch, ``/ws/2/artist/{mbid}`` url-rels) or are derived from the
verbatim live search response in ``tests/providers/musicbrainz/fixtures/``.
"""

from __future__ import annotations

import io
import json
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from music_friend.configuration import LocalConfig
from music_friend.domain import (
    Artist,
    IdentityConfidence,
    Release,
    ReleaseDatePrecision,
    ReleaseDiscovery,
    SourceLimitObservation,
    SourceLimitState,
    SourceReference,
    WatchlistAction,
    WatchlistOverride,
)
from music_friend.mcp import catalog_server
from music_friend.runtimes import cli, mcp_stdio
from music_friend.store import Catalog
from music_friend.tools import MusicFriendApplication
from music_friend.tools import refresh as refresh_module
from tests.providers.musicbrainz.live_fixtures import load_live_release_group_search

NOW = datetime(2026, 9, 2, 12, tzinfo=timezone.utc)
ARTIST_COUNT = 50
DEADLINE = 600.0
ENTRY_POINTS = ("cli", "mcp")


class FakeClock:
    """One monotonic timeline for the refresh deadline and every provider's pacing."""

    def __init__(self) -> None:
        self.value = 1_000.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        assert seconds >= 0
        self.sleeps.append(seconds)
        self.value += seconds


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[FakeClock]:
    fake = FakeClock()
    monkeypatch.setattr(time, "monotonic", fake.monotonic)
    monkeypatch.setattr(time, "sleep", fake.sleep)
    monkeypatch.setattr(refresh_module, "_deadline_clock", fake.monotonic)
    # The CLI derives its refresh lock (and default catalog) from platformdirs; keep
    # both inside this test's own directory.
    monkeypatch.setattr(cli, "user_data_path", lambda *_args, **_kwargs: tmp_path)
    yield fake


def _mbid(index: int) -> str:
    return f"00000000-0000-4000-8000-{index:012d}"


def _spotify_url(index: int) -> str:
    return f"https://open.spotify.com/artist/synthetic{index}"


def _seed(catalog_path: Path, count: int = ARTIST_COUNT) -> None:
    application = MusicFriendApplication(Catalog.open(catalog_path))
    try:
        for index in range(count):
            local_id = f"artist-{index}"
            application.put_artist(
                Artist(
                    local_id,
                    f"Synthetic Artist {index}",
                    (SourceReference("spotify", f"synthetic{index}", None, NOW),),
                    IdentityConfidence.SOURCE_ONLY,
                    NOW,
                )
            )
            application.put_watchlist_override(
                WatchlistOverride(local_id, WatchlistAction.ADD, NOW)
            )
    finally:
        application.close()


@dataclass
class MusicBrainzServer:
    """A fake MusicBrainz host that timestamps every request on the fake clock."""

    clock: FakeClock
    latency: float = 0.0
    fail_first_url_batch: bool = False
    requests: list[tuple[float, str]] = field(default_factory=list)
    status_codes: list[int] = field(default_factory=list)

    def handle(self, request: httpx.Request) -> httpx.Response:
        assert request.url.host == "musicbrainz.org"
        assert request.headers["user-agent"].startswith("music-friend/")
        self.requests.append((self.clock.value, request.url.path))
        self.clock.value += self.latency
        response = self._respond(request)
        self.status_codes.append(response.status_code)
        return response

    def _respond(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/ws/2/url":
            if self.fail_first_url_batch:
                self.fail_first_url_batch = False
                return httpx.Response(503, headers={"retry-after": "2"})
            resources = request.url.params.get_list("resource")
            urls = [
                {"resource": url, "relations": [{"artist": {"id": _mbid(_index_of(url))}}]}
                for url in resources
            ]
            return httpx.Response(200, json={"url-count": len(urls), "url-offset": 0, "urls": urls})
        if path == "/ws/2/release-group":
            live = load_live_release_group_search()
            return httpx.Response(200, json={**live, "count": 0, "release-groups": []})
        if path.startswith("/ws/2/artist/"):
            return httpx.Response(200, json={"relations": []})
        raise AssertionError(f"unexpected MusicBrainz path: {path}")


def _index_of(spotify_url: str) -> int:
    return int(spotify_url.rsplit("synthetic", 1)[1])


class _NoTicketmasterKey:
    def save(self, _key: object, _value: str) -> None:
        raise AssertionError("unused")

    def load(self, _key: object) -> str | None:
        return None

    def delete(self, _key: object) -> None:
        raise AssertionError("unused")


class _TicketmasterKey(_NoTicketmasterKey):
    def load(self, _key: object) -> str | None:
        return "synthetic-ticketmaster-value"


class _ConfigStore:
    def __init__(self, value: LocalConfig) -> None:
        self.value = value

    def load(self) -> LocalConfig:
        return self.value

    def save(self, value: LocalConfig) -> None:
        self.value = value


def _run_refresh(
    entry: str,
    kind: str,
    catalog_path: Path,
    connector_factory: Callable[[], httpx.BaseTransport],
    *,
    config: LocalConfig | None = None,
    credential_store: object | None = None,
) -> dict[str, object]:
    """Run one refresh through the named shipped entry point; return its public payload."""
    selected_config = LocalConfig() if config is None else config
    store = _NoTicketmasterKey() if credential_store is None else credential_store
    if entry == "cli":
        application = MusicFriendApplication(Catalog.open(catalog_path))
        stdout, stderr = io.StringIO(), io.StringIO()
        try:
            result = cli.run_cli(
                ["refresh", kind, "--json"],
                stdout=stdout,
                stderr=stderr,
                application=application,
                config_store=_ConfigStore(selected_config),
                secret_prompt=lambda _message: "",
                now=lambda: NOW,
                connector_factory=connector_factory,
                credential_store_factory=lambda: store,  # type: ignore[arg-type,return-value]
            )
        finally:
            application.close()
        assert stderr.getvalue() == ""
        payload = json.loads(stdout.getvalue())
        assert result == (3 if payload["status"] == "partial" else 0)
        assert isinstance(payload, dict)
        return payload
    captured: list[object] = []

    class _Server:
        def run(self, _transport: str) -> None:
            return None

    def create_server(_application: object, *, refresh: Callable[[str], object]) -> _Server:
        captured.append(refresh(kind))
        return _Server()

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(mcp_stdio, "create_music_server", create_server)
        patch.setattr(mcp_stdio, "_utc_now", lambda: NOW)
        mcp_stdio.run_catalog_stdio_session(
            config=selected_config,
            catalog_path=catalog_path,
            connector_factory=connector_factory,
            credential_store_factory=lambda: store,  # type: ignore[arg-type,return-value]
        )
    assert len(captured) == 1
    return catalog_server._refresh_result(captured[0])


def _metrics(payload: dict[str, object]) -> dict[str, int]:
    metrics = payload["metrics"]
    assert isinstance(metrics, list)
    return {item["kind"]: item["count"] for item in metrics}


def _gaps(times: list[float]) -> list[float]:
    return [later - earlier for earlier, later in zip(times, times[1:])]


@pytest.mark.parametrize("entry", ENTRY_POINTS)
def test_releases_refresh_of_fifty_artists_with_a_mapping_503_finishes_at_one_request_per_second(
    entry: str, clock: FakeClock, tmp_path: Path
) -> None:
    """AC: a 503 during mapping never slows MusicBrainz below 1 req/s, and discovery
    completes for every mapped artist in under 120 s of fake-clock time."""
    catalog_path = tmp_path / "catalog.sqlite3"
    _seed(catalog_path)
    server = MusicBrainzServer(clock, fail_first_url_batch=True)
    started = clock.value

    payload = _run_refresh(
        entry, "releases", catalog_path, lambda: httpx.MockTransport(server.handle)
    )

    elapsed = clock.value - started
    assert payload["status"] == "succeeded"
    assert elapsed < 120
    assert server.status_codes.count(503) == 1
    discovery = [at for at, path in server.requests if path == "/ws/2/release-group"]
    assert len(discovery) == ARTIST_COUNT
    # Steady 1 req/s: after the one Retry-After pause, no two consecutive MusicBrainz
    # requests are further apart than one second (and never closer, per MB policy).
    times = [at for at, _path in server.requests]
    retry_gap_index = server.status_codes.index(503)
    for index, gap in enumerate(_gaps(times)):
        assert gap >= 1.0 - 1e-6
        if index != retry_gap_index:
            assert gap <= 1.0 + 1e-6
    metrics = _metrics(payload)
    # Every MusicBrainz request, mapping included, is counted.
    assert metrics["source_requests"] == len(server.requests)
    assert metrics["limit_pauses"] == 1
    with Catalog.open(catalog_path) as catalog:
        for index in range(ARTIST_COUNT):
            assert catalog.get_release_check_cursor("musicbrainz", f"artist-{index}") is not None
        stored = catalog.get_source_limit("musicbrainz")
    assert stored is not None
    assert stored.window_calls == 1


@pytest.mark.parametrize("entry", ENTRY_POINTS)
@pytest.mark.parametrize(
    "saved",
    (
        # The row the live run left behind: an expired cooldown with a budget of one call
        # per 30-second Spotify-style window.
        SourceLimitObservation(
            "musicbrainz",
            SourceLimitState.COOLING_DOWN,
            NOW - timedelta(hours=2),
            NOW - timedelta(hours=1),
            True,
            1,
            1,
        ),
        SourceLimitObservation(
            "musicbrainz", SourceLimitState.AVAILABLE, NOW - timedelta(hours=2), None, False, 0, 1
        ),
    ),
)
def test_a_persisted_musicbrainz_limit_does_not_slow_the_next_run_below_one_request_per_second(
    entry: str, saved: SourceLimitObservation, clock: FakeClock, tmp_path: Path
) -> None:
    catalog_path = tmp_path / "catalog.sqlite3"
    _seed(catalog_path)
    with Catalog.open(catalog_path) as catalog:
        catalog.put_source_limit(saved)
    server = MusicBrainzServer(clock)
    started = clock.value

    payload = _run_refresh(
        entry, "releases", catalog_path, lambda: httpx.MockTransport(server.handle)
    )

    assert payload["status"] == "succeeded"
    assert clock.value - started < 120
    times = [at for at, _path in server.requests]
    assert len(times) == 1 + ARTIST_COUNT
    assert max(_gaps(times)) <= 1.0 + 1e-6


@pytest.mark.parametrize("entry", ENTRY_POINTS)
def test_a_slow_musicbrainz_run_returns_by_the_deadline_with_reason_deadline(
    entry: str, clock: FakeClock, tmp_path: Path
) -> None:
    """AC: the refresh returns by the 600 s deadline and says why it stopped."""
    catalog_path = tmp_path / "catalog.sqlite3"
    _seed(catalog_path)
    latency = 20.0
    server = MusicBrainzServer(clock, latency=latency)
    started = clock.value

    payload = _run_refresh(
        entry, "releases", catalog_path, lambda: httpx.MockTransport(server.handle)
    )

    assert payload["status"] == "partial"
    assert payload["reason"] == "deadline"
    # No request starts at or after the deadline; the last one in flight may finish it.
    assert all(at - started < DEADLINE for at, _path in server.requests)
    assert clock.value - started <= DEADLINE + latency
    assert _metrics(payload)["source_requests"] == len(server.requests)


@pytest.mark.parametrize("entry", ENTRY_POINTS)
def test_a_refresh_blocked_by_a_held_lock_says_already_running_and_when_it_goes_stale(
    entry: str, clock: FakeClock, tmp_path: Path
) -> None:
    catalog_path = tmp_path / "catalog.sqlite3"
    _seed(catalog_path, count=1)
    held_since = time.time()
    lock_path = tmp_path / "refresh.lock"
    lock_path.write_text(
        json.dumps({"created_at": held_since, "token": "synthetic-holder"}), encoding="utf-8"
    )

    payload = _run_refresh(
        entry,
        "releases",
        catalog_path,
        lambda: httpx.MockTransport(MusicBrainzServer(clock).handle),
    )

    assert payload == {
        "status": "partial",
        "reason": "already_running",
        "retry_after": datetime.fromtimestamp(held_since + DEADLINE, tz=timezone.utc).isoformat(),
    }


@pytest.mark.parametrize("entry", ENTRY_POINTS)
def test_events_refresh_counts_every_ticketmaster_request_and_reports_repairs_separately(
    entry: str, clock: FakeClock, tmp_path: Path
) -> None:
    """AC (issue comment): N Ticketmaster requests report source_requests == N, and a
    signal repaired from an earlier interrupted run is not counted as created."""
    catalog_path = tmp_path / "catalog.sqlite3"
    artist_count = 4
    _seed(catalog_path, count=artist_count)
    # An earlier run committed this release discovery and was killed before writing its
    # signal; the next refresh of any kind repairs it.
    with Catalog.open(catalog_path) as catalog:
        release = Release(
            "release:synthetic-orphan",
            "Synthetic Release",
            "album",
            date(2026, 8, 31),
            ReleaseDatePrecision.DAY,
            ("artist-0",),
            (SourceReference("musicbrainz", "synthetic-orphan", None, NOW),),
            NOW,
        )
        catalog.put_release(release)
        catalog.put_release_discovery(
            ReleaseDiscovery(
                release.local_id,
                "musicbrainz",
                "synthetic-orphan",
                "synthetic release",
                release.release_date,
                "release:synthetic-orphan-material",
                NOW - timedelta(days=1),
                NOW - timedelta(days=1),
            )
        )
    ticketmaster_requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "app.ticketmaster.com"
        ticketmaster_requests.append(request)
        # No attraction matches, so each artist costs exactly one request.
        return httpx.Response(200, json={})

    config = LocalConfig(
        event_country_code="US",
        event_postal_code="00000",
        event_radius=25,
        event_radius_unit="miles",
    )

    payload = _run_refresh(
        entry,
        "events",
        catalog_path,
        lambda: httpx.MockTransport(handle),
        config=config,
        credential_store=_TicketmasterKey(),
    )

    metrics = _metrics(payload)
    assert len(ticketmaster_requests) == artist_count
    assert metrics["source_requests"] == artist_count
    assert metrics["signals_repaired"] == 1
    assert "signals_created" not in metrics
