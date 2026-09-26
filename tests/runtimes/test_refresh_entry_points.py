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
    Explanation,
    ExplanationReason,
    ExplanationReasonKind,
    IdentityConfidence,
    InboxState,
    Release,
    ReleaseDatePrecision,
    ReleaseDiscovery,
    SignalKind,
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
from tests.providers.musicbrainz.live_fixtures import (
    LIVE_SEARCH_ARTIST_MBID,
    load_live_release_group_search,
)

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
    #: When set, every Nth ``/ws/2/release-group`` request (1-indexed) gets a 503
    #: instead of a real response: MusicBrainz's own documented transient load
    #: shedding, repeated across the run (issue #54).
    fail_every_nth_release: int | None = None
    requests: list[tuple[float, str]] = field(default_factory=list)
    status_codes: list[int] = field(default_factory=list)
    _release_calls: int = 0

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
            self._release_calls += 1
            if (
                self.fail_every_nth_release is not None
                and self._release_calls % self.fail_every_nth_release == 0
            ):
                return httpx.Response(503, headers={"retry-after": "2"})
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
def test_releases_refresh_of_fifty_artists_survives_repeated_musicbrainz_503s(
    entry: str, clock: FakeClock, tmp_path: Path
) -> None:
    """AC (issue #54): a fake MusicBrainz transport that 503s on every 7th release-group
    request across a 50-artist watchlist still finishes every artist inside the refresh
    deadline with ``status: succeeded``. ``limit_pauses`` reflects every pause (more than
    Spotify's shared two-pause budget would ever allow), and no two MusicBrainz requests
    land closer together than 1 second."""
    catalog_path = tmp_path / "catalog.sqlite3"
    _seed(catalog_path)
    server = MusicBrainzServer(clock, fail_every_nth_release=7)
    started = clock.value

    payload = _run_refresh(
        entry, "releases", catalog_path, lambda: httpx.MockTransport(server.handle)
    )

    elapsed = clock.value - started
    assert payload["status"] == "succeeded"
    assert elapsed < DEADLINE
    successful_discovery = [
        at
        for (at, path), status in zip(server.requests, server.status_codes)
        if path == "/ws/2/release-group" and status == 200
    ]
    assert len(successful_discovery) == ARTIST_COUNT
    pause_count = server.status_codes.count(503)
    # 50 artists / every 7th call: more pauses than Spotify's shared _MAX_LIMIT_PAUSES (2)
    # would ever tolerate, proving MusicBrainz's pause budget is no longer shared with it.
    assert pause_count > 2
    metrics = _metrics(payload)
    assert metrics["limit_pauses"] == pause_count
    times = [at for at, _path in server.requests]
    assert all(gap >= 1.0 - 1e-6 for gap in _gaps(times))
    with Catalog.open(catalog_path) as catalog:
        for index in range(ARTIST_COUNT):
            assert catalog.get_release_check_cursor("musicbrainz", f"artist-{index}") is not None


@pytest.mark.parametrize("entry", ENTRY_POINTS)
def test_musicbrainz_mapping_and_discovery_share_one_pacing_state(
    entry: str, clock: FakeClock, tmp_path: Path
) -> None:
    """AC (issue #54): mapping (the ``/ws/2/url`` batch) and discovery (per-artist
    ``/ws/2/release-group`` calls) run through exactly one ``_PacedSource`` per refresh,
    so their requests share one pacing state and clock. Inter-request spacing stays at
    or above 1 s across both phases, and only one MusicBrainz ``_PacedSource`` is ever
    constructed for the run."""
    catalog_path = tmp_path / "catalog.sqlite3"
    _seed(catalog_path)
    server = MusicBrainzServer(clock)
    created: list[refresh_module._PacedSource] = []
    real_init = refresh_module._PacedSource.__init__

    def _tracking_init(self: refresh_module._PacedSource, *args: object, **kwargs: object) -> None:
        real_init(self, *args, **kwargs)  # type: ignore[arg-type]
        if kwargs.get("source_name") == "musicbrainz":
            created.append(self)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(refresh_module._PacedSource, "__init__", _tracking_init)
        payload = _run_refresh(
            entry, "releases", catalog_path, lambda: httpx.MockTransport(server.handle)
        )

    assert payload["status"] == "succeeded"
    assert len(created) == 1
    mapping_calls = [at for at, path in server.requests if path == "/ws/2/url"]
    discovery_calls = [at for at, path in server.requests if path == "/ws/2/release-group"]
    assert mapping_calls and discovery_calls
    times = [at for at, _path in server.requests]
    assert all(gap >= 1.0 - 1e-6 for gap in _gaps(times))


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


@pytest.mark.parametrize("entry", ENTRY_POINTS)
def test_map_then_discover_within_one_refresh_adds_the_reference_and_one_inbox_item(
    entry: str, clock: FakeClock, tmp_path: Path
) -> None:
    """Issue #48 AC1: starting from a watchlisted artist with only a Spotify
    SourceReference, a single releases refresh maps it to musicbrainz via /ws/2/url
    and then discovers its releases via /ws/2/release-group, in that order, adding the
    musicbrainz reference and creating exactly one inbox item."""
    catalog_path = tmp_path / "catalog.sqlite3"
    _seed(catalog_path, count=1)
    live = load_live_release_group_search()
    one_release_group = live["release-groups"][:1]
    request_log: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "musicbrainz.org"
        path = request.url.path
        request_log.append(path)
        if path == "/ws/2/url":
            resources = request.url.params.get_list("resource")
            assert resources == [_spotify_url(0)]
            # The recorded release-group fixture's artist-credit is the "Various
            # Artists" MBID it was actually queried for, so the mapping must resolve
            # this artist to that same MBID for normalize_release_group to attach it.
            urls = [
                {
                    "resource": resources[0],
                    "relations": [{"artist": {"id": LIVE_SEARCH_ARTIST_MBID}}],
                }
            ]
            return httpx.Response(200, json={"url-count": 1, "url-offset": 0, "urls": urls})
        if path == "/ws/2/release-group":
            return httpx.Response(
                200,
                json={
                    **live,
                    "count": len(one_release_group),
                    "release-groups": one_release_group,
                },
            )
        raise AssertionError(f"unexpected MusicBrainz path: {path}")

    payload = _run_refresh(entry, "releases", catalog_path, lambda: httpx.MockTransport(handle))

    assert payload["status"] == "succeeded"
    assert request_log == ["/ws/2/url", "/ws/2/release-group"]
    assert _metrics(payload)["signals_created"] == 1
    with Catalog.open(catalog_path) as catalog:
        artist = catalog.get_artist("artist-0")
    assert artist is not None
    assert any(
        reference.source == "musicbrainz" and reference.native_id == LIVE_SEARCH_ARTIST_MBID
        for reference in artist.source_refs
    )


@pytest.mark.parametrize("entry", ENTRY_POINTS)
def test_release_source_unmapped_metric_surfaces_when_mapping_finds_nobody(
    entry: str, clock: FakeClock, tmp_path: Path
) -> None:
    """Issue #48 AC2: a releases refresh in which identity mapping resolved nobody
    must report a non-zero release_source_unmapped metric -- in the refresh result and
    in music_status -- rather than a signal-free 'succeeded'."""
    catalog_path = tmp_path / "catalog.sqlite3"
    _seed(catalog_path, count=2)

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "musicbrainz.org"
        if request.url.path == "/ws/2/url":
            return httpx.Response(200, json={"url-count": 0, "url-offset": 0, "urls": []})
        if request.url.path == "/ws/2/artist":
            # The url-rel batch resolved nobody, so mapping falls back to a name
            # search per artist; no hits either, so every artist stays unmapped.
            return httpx.Response(200, json={"artists": []})
        raise AssertionError(f"unexpected MusicBrainz path: {request.url.path}")

    payload = _run_refresh(entry, "releases", catalog_path, lambda: httpx.MockTransport(handle))

    assert payload["status"] == "succeeded"
    assert _metrics(payload)["release_source_unmapped"] == 2

    application = MusicFriendApplication(Catalog.open(catalog_path))
    try:
        latest = application.list_refresh_runs(limit=1)
    finally:
        application.close()
    assert len(latest) == 1
    status_metrics = {metric.kind.value: metric.count for metric in latest[0].summary.metrics}
    assert status_metrics["release_source_unmapped"] == 2


def test_refresh_all_through_cli_still_opens_the_catalog_source(
    clock: FakeClock, tmp_path: Path
) -> None:
    """Issue #48 AC2 regression: a 'refresh all' through cli.run_cli must still build
    and pass a real catalog source into refresh_once -- not silently fall back to
    ``source=None``, which refresh_once now rejects for any kind that runs the
    catalog component."""
    catalog_path = tmp_path / "catalog.sqlite3"
    connector_calls = 0

    def factory() -> httpx.BaseTransport:
        nonlocal connector_calls
        connector_calls += 1
        return httpx.MockTransport(lambda _request: httpx.Response(401, json={}))

    config = LocalConfig(spotify_client_id="synthetic-client-id")
    application = MusicFriendApplication(Catalog.open(catalog_path))
    stdout, stderr = io.StringIO(), io.StringIO()
    try:
        cli.run_cli(
            ["refresh", "all", "--json"],
            stdout=stdout,
            stderr=stderr,
            application=application,
            config_store=_ConfigStore(config),
            secret_prompt=lambda _message: "",
            now=lambda: NOW,
            connector_factory=factory,
            credential_store_factory=lambda: _NoTicketmasterKey(),
        )
    finally:
        application.close()

    payload = json.loads(stdout.getvalue())
    assert payload["kind"] == "all"
    # The connector factory was actually invoked to build the catalog source's
    # transport; refresh_once would have raised "source is required for catalog
    # refresh" before any of this if the CLI had passed source=None instead.
    assert connector_calls >= 1


def test_same_source_repeat_through_cli_creates_no_new_inbox_item(
    clock: FakeClock, tmp_path: Path
) -> None:
    """Issue #50 AC: the same real-world release, re-seen through ``music-friend
    refresh releases`` on later runs spaced past the freshness TTL, creates no new
    inbox item -- proved through ``cli.run_cli``, the shipped entry point, not
    ``refresh_once`` directly.

    Three ``music-friend refresh releases`` runs against a watchlisted artist already
    mapped to MusicBrainz, each returning the identical recorded release-group. The
    first creates the one inbox item; the second and third runs are far enough apart
    (21 and 42 days) that ``FRESHNESS_TTL`` does not skip them, so MusicBrainz is
    genuinely re-queried each time and returns the same release both times.
    """
    catalog_path = tmp_path / "catalog.sqlite3"
    _seed(catalog_path, count=1)
    live = load_live_release_group_search()
    one_release_group = live["release-groups"][:1]

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "musicbrainz.org"
        path = request.url.path
        if path == "/ws/2/url":
            resources = request.url.params.get_list("resource")
            urls = [
                {
                    "resource": resources[0],
                    "relations": [{"artist": {"id": LIVE_SEARCH_ARTIST_MBID}}],
                }
            ]
            return httpx.Response(200, json={"url-count": 1, "url-offset": 0, "urls": urls})
        if path == "/ws/2/release-group":
            return httpx.Response(
                200,
                json={
                    **live,
                    "count": len(one_release_group),
                    "release-groups": one_release_group,
                },
            )
        if path.startswith("/ws/2/artist/"):
            # A later run's map-then-discover hardening pass (#48) also validates the
            # already-mapped artist's url-rels; an empty relation list is a harmless,
            # already-mapped no-op here.
            return httpx.Response(200, json={"relations": []})
        raise AssertionError(f"unexpected MusicBrainz path: {path}")

    def _run_at(now: datetime) -> dict[str, object]:
        application = MusicFriendApplication(Catalog.open(catalog_path))
        stdout, stderr = io.StringIO(), io.StringIO()
        try:
            cli.run_cli(
                ["refresh", "releases", "--json"],
                stdout=stdout,
                stderr=stderr,
                application=application,
                config_store=_ConfigStore(LocalConfig()),
                secret_prompt=lambda _message: "",
                now=lambda: now,
                connector_factory=lambda: httpx.MockTransport(handle),
                credential_store_factory=lambda: _NoTicketmasterKey(),
            )
        finally:
            application.close()
        assert stderr.getvalue() == ""
        payload = json.loads(stdout.getvalue())
        assert isinstance(payload, dict)
        return payload

    first = _run_at(NOW)
    assert first["status"] == "succeeded"
    assert _metrics(first)["signals_created"] == 1

    second = _run_at(NOW + timedelta(days=21))
    assert second["status"] == "succeeded"
    assert _metrics(second).get("signals_created", 0) == 0

    third = _run_at(NOW + timedelta(days=42))
    assert third["status"] == "succeeded"
    assert _metrics(third).get("signals_created", 0) == 0

    application = MusicFriendApplication(Catalog.open(catalog_path))
    try:
        entries = application.list_inbox_entries(None, limit=10)
        signals = application.list_signals(None, limit=10)
    finally:
        application.close()
    assert len(entries) == 1
    assert len(signals) == 1


@pytest.mark.parametrize(
    "seeded_state", [InboxState.UNREAD, InboxState.SAVED, InboxState.DISMISSED]
)
def test_v1_signal_upgrade_safety_creates_no_duplicate_and_preserves_inbox_state(
    clock: FakeClock, tmp_path: Path, seeded_state: InboxState
) -> None:
    """Issue #50 round 2, item 2: upgrading ``material_version`` from the pre-#53 v1
    shape (included ``observed_at``) to the content-only v2 shape must not treat a
    real catalog's existing v1-format signals as missing and re-record them.

    Builds the pre-existing state exactly as the base branch (625049e, pre-#53) would
    have written it: runs one real ``cli.run_cli`` refresh against a recorded
    MusicBrainz fixture (so the release/discovery/signal/inbox rows are the actual
    current-code output, not hand-built), then downgrades the created signal's
    ``material_version`` in place to the exact v1 hash the base branch's
    ``_material_version`` would have produced for that same signal -- the only piece
    that changed -- and sets the inbox entry to the given historical state. Two more
    refreshes past the freshness TTL must then create zero new signals, zero new inbox
    entries, and leave the seeded state untouched.
    """
    catalog_path = tmp_path / "catalog.sqlite3"
    _seed(catalog_path, count=1)
    live = load_live_release_group_search()
    one_release_group = live["release-groups"][:1]

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "musicbrainz.org"
        path = request.url.path
        if path == "/ws/2/url":
            resources = request.url.params.get_list("resource")
            urls = [
                {
                    "resource": resources[0],
                    "relations": [{"artist": {"id": LIVE_SEARCH_ARTIST_MBID}}],
                }
            ]
            return httpx.Response(200, json={"url-count": 1, "url-offset": 0, "urls": urls})
        if path == "/ws/2/release-group":
            return httpx.Response(
                200,
                json={
                    **live,
                    "count": len(one_release_group),
                    "release-groups": one_release_group,
                },
            )
        if path.startswith("/ws/2/artist/"):
            return httpx.Response(200, json={"relations": []})
        raise AssertionError(f"unexpected MusicBrainz path: {path}")

    def _run_at(now: datetime) -> dict[str, object]:
        application = MusicFriendApplication(Catalog.open(catalog_path))
        stdout, stderr = io.StringIO(), io.StringIO()
        try:
            cli.run_cli(
                ["refresh", "releases", "--json"],
                stdout=stdout,
                stderr=stderr,
                application=application,
                config_store=_ConfigStore(LocalConfig()),
                secret_prompt=lambda _message: "",
                now=lambda: now,
                connector_factory=lambda: httpx.MockTransport(handle),
                credential_store_factory=lambda: _NoTicketmasterKey(),
            )
        finally:
            application.close()
        assert stderr.getvalue() == ""
        payload = json.loads(stdout.getvalue())
        assert isinstance(payload, dict)
        return payload

    # 1. A real refresh with today's (v2, content-only) code creates the actual
    #    release/discovery/signal/inbox rows from the recorded fixture.
    first = _run_at(NOW)
    assert first["status"] == "succeeded"
    assert _metrics(first)["signals_created"] == 1

    application = MusicFriendApplication(Catalog.open(catalog_path))
    try:
        seeded_signal = application.list_signals(SignalKind.RELEASE, limit=1)[0]
        seeded_entry = application.list_inbox_entries(None, limit=1)[0]
        assert seeded_entry.signal_local_id == seeded_signal.local_id
        release = application.get_release(seeded_signal.record_local_id)
        assert release is not None
        artist = application.get_artist(release.artist_refs[0])
        assert artist is not None

        # 2. Downgrade this real signal to the exact v1 hash the pre-#53 base branch
        #    would have produced for it -- same reason/material/observed_at, only the
        #    hash formula changes (v1 includes observed_at; v2 does not).
        material = refresh_module._release_material(release)
        explanation = Explanation(
            (
                ExplanationReason(ExplanationReasonKind.MONITORED_ARTIST, artist.display_name),
                ExplanationReason(ExplanationReasonKind.NEW_RELEASE, release.title),
            )
        )
        legacy_material_version = refresh_module._legacy_v1_material_version(
            SignalKind.RELEASE, material, explanation, seeded_signal.observed_at
        )
        connection = application._catalog._require_connection()
        connection.execute(
            "UPDATE signals SET material_version = ? WHERE local_id = ?",
            (legacy_material_version, seeded_signal.local_id),
        )
        connection.commit()

        # 3. Set the inbox entry to the historical state under test, exactly as a
        #    real inbox row could already carry before this fix ever ran.
        refresh_module.update_inbox_state(
            application, seeded_entry.local_id, seeded_state, updated_at=NOW
        )
    finally:
        application.close()

    # 4. Two more refreshes, past the freshness TTL each time so MusicBrainz is
    #    genuinely re-queried and returns the identical fixture release both times.
    second = _run_at(NOW + timedelta(days=21))
    assert second["status"] == "succeeded"
    assert _metrics(second).get("signals_created", 0) == 0

    third = _run_at(NOW + timedelta(days=42))
    assert third["status"] == "succeeded"
    assert _metrics(third).get("signals_created", 0) == 0

    application = MusicFriendApplication(Catalog.open(catalog_path))
    try:
        entries = application.list_inbox_entries(None, limit=10)
        signals = application.list_signals(None, limit=10)
    finally:
        application.close()
    assert len(signals) == 1
    assert len(entries) == 1
    assert entries[0].state is seeded_state
