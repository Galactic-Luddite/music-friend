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
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
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
from music_friend.providers import (
    Capability,
    HealthStatus,
    Page,
    ProviderCapabilities,
    ProviderHealth,
)
from music_friend.runtimes import cli, mcp_stdio
from music_friend.store import Catalog
from music_friend.tools import MusicFriendApplication
from music_friend.tools import refresh as refresh_module
from music_friend.tools.release_observation import content_version_of
from tests.providers.musicbrainz.live_fixtures import (
    LIVE_BROWSE_RELEASE_GROUP_ID,
    LIVE_SEARCH_ARTIST_MBID,
    load_live_release_browse,
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
        if path == "/ws/2/release":
            assert request.url.params["release-group"] == LIVE_BROWSE_RELEASE_GROUP_ID
            return httpx.Response(200, json=load_live_release_browse())
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
    now: datetime = NOW,
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
                now=lambda: now,
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

    def create_server(
        _application: object,
        *,
        refresh: Callable[[str], object],
        refresh_status: Callable[[], bool] | None = None,
    ) -> _Server:
        captured.append(refresh(kind))
        return _Server()

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(mcp_stdio, "create_music_server", create_server)
        patch.setattr(mcp_stdio, "_utc_now", lambda: now)
        mcp_stdio.run_catalog_stdio_session(
            config=selected_config,
            catalog_path=catalog_path,
            connector_factory=connector_factory,
            credential_store_factory=lambda: store,  # type: ignore[arg-type,return-value]
        )
    assert len(captured) == 1
    return catalog_server._refresh_result(captured[0])


def _pre_62_release_material(release: Release) -> dict[str, object]:
    """The release material shape signals were hashed from before issue #62."""
    return {
        "artist_refs": release.artist_refs,
        "date_precision": release.date_precision.value,
        "release_date": release.release_date.isoformat(),
        "release_type": release.release_type,
        "source_refs": tuple(
            (reference.source, reference.native_id, reference.canonical_url)
            for reference in release.source_refs
        ),
        "title": release.title,
    }


def _pre_53_v1_material_version(
    kind: SignalKind, material: object, explanation: Explanation, observed_at: datetime
) -> str:
    """The exact pre-#53 v1 ``material_version`` a real catalog may still hold."""
    value = {
        "kind": kind.value,
        "material": material,
        "observed_at": observed_at.isoformat(),
        "reasons": tuple((reason.kind.value, reason.detail) for reason in explanation.reasons),
        "version": 1,
    }
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return f"material:{sha256(encoded.encode('utf-8')).hexdigest()}"


def one_release_group_handler() -> Callable[[httpx.Request], httpx.Response]:
    """A MusicBrainz host that maps artist-0 and always returns one recorded release-group."""
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
                json={**live, "count": len(one_release_group), "release-groups": one_release_group},
            )
        if path == "/ws/2/release":
            assert request.url.params["release-group"] == LIVE_BROWSE_RELEASE_GROUP_ID
            return httpx.Response(200, json=load_live_release_browse())
        if path.startswith("/ws/2/artist/"):
            return httpx.Response(200, json={"relations": []})
        if path == "/ws/2/release":
            assert request.url.params["release-group"] == LIVE_BROWSE_RELEASE_GROUP_ID
            return httpx.Response(200, json=load_live_release_browse())
        raise AssertionError(f"unexpected MusicBrainz path: {path}")

    return handle


def attach_a_second_source(catalog_path: Path) -> Release:
    """Attach a synthetic Deezer reference to artist-0's one release by a real cross-source
    discovery merge (issue #42), and return the merged release."""
    application = MusicFriendApplication(Catalog.open(catalog_path))
    try:
        rows = (
            application._catalog._require_connection()
            .execute("SELECT local_id FROM releases")
            .fetchall()
        )
        assert len(rows) == 1
        release = application.get_release(str(rows[0][0]))
        assert release is not None
        artist = application.get_artist("artist-0")
        assert artist is not None
        application.put_artist(
            replace(
                artist,
                source_refs=artist.source_refs
                + (SourceReference("deezer", "synthetic-deezer-artist-0", None, NOW),),
            )
        )
        deezer = _SingleReleaseSource(
            Release(
                "release:deezer:synthetic-album-0",
                release.title,
                release.release_type,
                release.release_date,
                release.date_precision,
                release.artist_refs,
                (SourceReference("deezer", "synthetic-album-0", None, NOW),),
                NOW,
            )
        )
        result = application.discover_releases("deezer", deezer, checked_at=NOW)
        assert result.artists[0].candidates == ()
        merged = application.get_release(release.local_id)
        assert merged is not None
        assert {reference.source for reference in merged.source_refs} == {"musicbrainz", "deezer"}
        assert application.get_release("release:deezer:synthetic-album-0") is None
        return merged
    finally:
        application.close()


class _SingleReleaseSource:
    """A release source that returns one fixed release for any artist."""

    def __init__(self, release: Release) -> None:
        self.release = release

    def capabilities(self) -> ProviderCapabilities:
        allowed = frozenset(Capability)
        return ProviderCapabilities(allowed, allowed)

    def health(self) -> ProviderHealth:
        return ProviderHealth(HealthStatus.HEALTHY, self.capabilities())

    def search_artists(self, _query: str, _limit: int) -> Page[Artist]:
        raise AssertionError("unused")

    def followed_artists(self, _cursor: str | None = None) -> Page[Artist]:
        raise AssertionError("unused")

    def saved_items(self, _cursor: str | None = None) -> object:
        raise AssertionError("unused")

    def top_items(self, _time_range: str, _limit: int) -> object:
        raise AssertionError("unused")

    def top_artists(self, _time_range: str, _limit: int) -> Page[Artist]:
        raise AssertionError("unused")

    def recent_releases(
        self, _artist_refs: object, _since: datetime, _cursor: str | None = None
    ) -> Page[Release]:
        return Page((self.release,), None)


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
        if path == "/ws/2/release":
            assert request.url.params["release-group"] == LIVE_BROWSE_RELEASE_GROUP_ID
            return httpx.Response(200, json=load_live_release_browse())
        raise AssertionError(f"unexpected MusicBrainz path: {path}")

    payload = _run_refresh(entry, "releases", catalog_path, lambda: httpx.MockTransport(handle))

    assert payload["status"] == "succeeded"
    # One link harvest for the one new release group (issue #63).
    assert request_log == ["/ws/2/url", "/ws/2/release-group", "/ws/2/release"]
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
        if path == "/ws/2/release":
            assert request.url.params["release-group"] == LIVE_BROWSE_RELEASE_GROUP_ID
            return httpx.Response(200, json=load_live_release_browse())
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
        if path == "/ws/2/release":
            assert request.url.params["release-group"] == LIVE_BROWSE_RELEASE_GROUP_ID
            return httpx.Response(200, json=load_live_release_browse())
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
        assert seeded_entry.latest_signal_local_id == seeded_signal.local_id
        release = application.get_release(seeded_signal.record_local_id)
        assert release is not None
        artist = application.get_artist(release.artist_refs[0])
        assert artist is not None

        # 2. Downgrade this real signal to the exact v1 hash the pre-#53 base branch
        #    would have produced for it -- same reason/material/observed_at, only the
        #    hash formula changes (v1 includes observed_at; v2 does not).
        material = _pre_62_release_material(release)
        explanation = Explanation(
            (
                ExplanationReason(ExplanationReasonKind.MONITORED_ARTIST, artist.display_name),
                ExplanationReason(ExplanationReasonKind.NEW_RELEASE, release.title),
            )
        )
        legacy_material_version = _pre_53_v1_material_version(
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


@pytest.mark.parametrize("seeded_state", [InboxState.DISMISSED, InboxState.SAVED])
def test_re_observed_release_never_reopens_a_decided_inbox_entry_through_cli(
    clock: FakeClock, tmp_path: Path, seeded_state: InboxState
) -> None:
    """Design invariant: "re-observation never changes state."

    A release the user already decided (saved/dismissed) can still change on a later
    refresh -- new provenance is attached, or a genuine material change is picked up --
    without the schema allowing a second inbox item (``UNIQUE (kind, subject_local_id)``).
    The fix must repoint the existing entry's ``latest_signal_local_id`` without ever
    reopening it to ``unread``: a routine refresh that happens to pick up new information
    about an already-decided release must never surface it again as if undecided --
    that is the exact #57 duplicate-inbox regression this issue closes, and reopening
    to unread on a routine re-observation would be a quieter version of the same bug.

    Proved through ``cli.run_cli`` (``music-friend refresh releases``), the shipped entry
    point, against the same recorded MusicBrainz fixture the sibling tests in this file
    use. This test covers a genuine *content* change -- a title edit the fold logic does
    not absorb, picked up on a later run -- against an already-decided subject: it gets a
    new ``updated_release`` signal and the one entry is repointed at it, state unchanged.
    The provenance case -- a second source attached to a decided release, followed by a
    refresh whose repair pass actually runs -- is covered separately by
    ``test_second_source_on_a_decided_release_survives_repair_through_cli``, where no new
    signal may be created at all because the content did not change.
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
        if path == "/ws/2/release":
            assert request.url.params["release-group"] == LIVE_BROWSE_RELEASE_GROUP_ID
            return httpx.Response(200, json=load_live_release_browse())
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

    # 1. A real refresh creates the one release/signal/inbox row from the fixture.
    first = _run_at(NOW)
    assert first["status"] == "succeeded"
    assert _metrics(first)["signals_created"] == 1

    application = MusicFriendApplication(Catalog.open(catalog_path))
    try:
        seeded_entry = application.list_inbox_entries(None, limit=1)[0]
        seeded_signal = application.list_signals(None, limit=1)[0]
        assert seeded_entry.latest_signal_local_id == seeded_signal.local_id
        # 2. The user decides: saved or dismissed.
        refresh_module.update_inbox_state(
            application, seeded_entry.local_id, seeded_state, updated_at=NOW
        )
    finally:
        application.close()

    # 3. A later refresh picks up a genuine material change (a title edit the fold
    #    logic does not absorb) for the same real-world release -- attaching new
    #    provenance about it, same as a second source would.
    changed_title = f"{one_release_group[0]['title']} (Live Recording)"
    one_release_group[0] = {**one_release_group[0], "title": changed_title}

    second = _run_at(NOW + timedelta(hours=25))
    assert second["status"] == "succeeded"
    # A genuinely new signal is created for the changed material -- that is expected
    # and correct; only the *inbox entry* must stay singular and keep its decision.
    assert _metrics(second)["signals_created"] == 1

    application = MusicFriendApplication(Catalog.open(catalog_path))
    try:
        entries = application.list_inbox_entries(None, limit=10)
        signals = application.list_signals(None, limit=10)
        assert len(signals) == 2
        assert len(entries) == 1
        entry = entries[0]
        assert entry.local_id == seeded_entry.local_id
        # The user's decision survives the re-observation untouched.
        assert entry.state is seeded_state
        # ...but the entry now points at the fresh signal, not the stale one.
        assert entry.latest_signal_local_id != seeded_signal.local_id
        assert entry.latest_signal_local_id in {signal.local_id for signal in signals}
    finally:
        application.close()


@pytest.mark.parametrize("decided", [InboxState.DISMISSED, InboxState.SAVED])
def test_second_source_on_a_decided_release_survives_repair_through_cli(
    clock: FakeClock, tmp_path: Path, decided: InboxState
) -> None:
    """Issue #57 regression through ``music-friend refresh releases --json``.

    A release the user decided gets a second source through a real cross-source merge. An
    earlier interrupted run also left a second release committed without its signal, so the
    next refresh's repair pass genuinely executes (it repairs that orphan). The decided
    release must keep exactly one inbox entry with its state, gain no new signal (its
    content did not change), and the run must succeed without an IntegrityError.
    """
    catalog_path = tmp_path / "catalog.sqlite3"
    _seed(catalog_path, count=1)
    handle = one_release_group_handler()
    first = _run_refresh("cli", "releases", catalog_path, lambda: httpx.MockTransport(handle))
    assert first["status"] == "succeeded"
    assert _metrics(first)["signals_created"] == 1

    application = MusicFriendApplication(Catalog.open(catalog_path))
    try:
        (entry,) = application.list_inbox_entries(None, limit=10)
        refresh_module.update_inbox_state(application, entry.local_id, decided, updated_at=NOW)
        (decided_entry,) = application.list_inbox_entries(None, limit=10)
    finally:
        application.close()
    merged = attach_a_second_source(catalog_path)
    with Catalog.open(catalog_path) as catalog:
        orphan = Release(
            "release:synthetic-orphan",
            "Synthetic Orphan Release",
            "album",
            date(2026, 8, 31),
            ReleaseDatePrecision.DAY,
            ("artist-0",),
            (SourceReference("musicbrainz", "synthetic-orphan", None, NOW),),
            NOW,
        )
        catalog.put_release(orphan)
        catalog.put_release_discovery(
            ReleaseDiscovery(
                orphan.local_id,
                "musicbrainz",
                "synthetic-orphan",
                "synthetic orphan release",
                orphan.release_date,
                "release:synthetic-orphan-material",
                NOW,
                NOW,
            )
        )
        signals_before = catalog.list_signals(SignalKind.RELEASE, limit=10)

    second = _run_refresh(
        "cli",
        "releases",
        catalog_path,
        lambda: httpx.MockTransport(handle),
        now=NOW + timedelta(days=21),
    )

    metrics = _metrics(second)
    assert second["status"] == "succeeded"
    assert metrics["signals_repaired"] == 1
    assert metrics.get("signals_created", 0) == 0
    assert metrics.get("failures", 0) == 0
    with Catalog.open(catalog_path) as catalog:
        entries = catalog.list_inbox_entries(None, limit=10)
        merged_entries = [
            item for item in entries if item.subject_local_id == merged.subject_local_id
        ]
        assert len(merged_entries) == 1
        assert merged_entries[0].local_id == decided_entry.local_id
        assert merged_entries[0].state is decided
        assert merged_entries[0].latest_signal_local_id == decided_entry.latest_signal_local_id
        assert merged_entries[0].updated_at == decided_entry.updated_at
        signals_after = catalog.list_signals(SignalKind.RELEASE, limit=10)
        assert [
            signal for signal in signals_after if signal.record_local_id == merged.local_id
        ] == [signal for signal in signals_before if signal.record_local_id == merged.local_id]
        assert len(signals_after) == len(signals_before) + 1
        orphan_entry = catalog.get_inbox_entry_for_subject(SignalKind.RELEASE, orphan.local_id)
        assert orphan_entry is not None and orphan_entry.state is InboxState.UNREAD
        stored = catalog.get_release(merged.local_id)
        assert stored is not None
        assert {reference.source for reference in stored.source_refs} == {"musicbrainz", "deezer"}


class _ProcessKilled(BaseException):
    """Stands in for the process dying: nothing in the refresh may catch it."""


def _retitled_release_group_handler(
    title: list[str],
) -> Callable[[httpx.Request], httpx.Response]:
    """``one_release_group_handler`` whose one release-group reports ``title[0]`` when set."""
    handle = one_release_group_handler()

    def retitle(request: httpx.Request) -> httpx.Response:
        response = handle(request)
        if request.url.path != "/ws/2/release-group" or not title:
            return response
        body = json.loads(response.content)
        body["release-groups"] = [{**body["release-groups"][0], "title": title[0]}]
        return httpx.Response(200, json=body)

    return retitle


@pytest.mark.parametrize("decided", [InboxState.DISMISSED, InboxState.SAVED])
def test_crash_mid_update_of_a_listed_release_is_completed_next_refresh_through_cli(
    clock: FakeClock, tmp_path: Path, decided: InboxState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #70 through ``music-friend refresh releases --json``.

    A release the user decided changes title. The refresh commits the new content, then the
    process dies before the signal and inbox write. The next refresh completes the write: one
    updated signal, the one entry repointed at it with the user's state, status succeeded.
    """
    catalog_path = tmp_path / "catalog.sqlite3"
    _seed(catalog_path, count=1)
    title: list[str] = []
    handle = _retitled_release_group_handler(title)
    first = _run_refresh("cli", "releases", catalog_path, lambda: httpx.MockTransport(handle))
    assert first["status"] == "succeeded"
    assert _metrics(first)["signals_created"] == 1
    application = MusicFriendApplication(Catalog.open(catalog_path))
    try:
        (entry,) = application.list_inbox_entries(None, limit=10)
        (original_signal,) = application.list_signals(None, limit=10)
        refresh_module.update_inbox_state(application, entry.local_id, decided, updated_at=NOW)
    finally:
        application.close()

    title.append("Synthetic Retitled Release")

    def killed(*_args: object, **_kwargs: object) -> object:
        raise _ProcessKilled

    crashed_at = NOW + timedelta(days=2)
    with monkeypatch.context() as patch:
        patch.setattr(refresh_module, "record_release_observation", killed)
        application = MusicFriendApplication(Catalog.open(catalog_path))
        try:
            with pytest.raises(_ProcessKilled):
                cli.run_cli(
                    ["refresh", "releases", "--json"],
                    stdout=io.StringIO(),
                    stderr=io.StringIO(),
                    application=application,
                    config_store=_ConfigStore(LocalConfig()),
                    secret_prompt=lambda _message: "",
                    now=lambda: crashed_at,
                    connector_factory=lambda: httpx.MockTransport(handle),
                    credential_store_factory=lambda: _NoTicketmasterKey(),
                )
        finally:
            application.close()
    with Catalog.open(catalog_path) as catalog:
        (release_local_id,) = (
            str(row[0])
            for row in catalog._require_connection()
            .execute("SELECT local_id FROM releases")
            .fetchall()
        )
        crashed = catalog.get_release(release_local_id)
        assert crashed is not None and crashed.title == "Synthetic Retitled Release"
        assert catalog.list_signals(SignalKind.RELEASE, limit=10) == (original_signal,)

    second = _run_refresh(
        "cli",
        "releases",
        catalog_path,
        lambda: httpx.MockTransport(handle),
        now=crashed_at + timedelta(hours=1),
    )

    metrics = _metrics(second)
    assert second["status"] == "succeeded"
    assert metrics["signals_repaired"] == 1
    assert metrics.get("failures", 0) == 0
    with Catalog.open(catalog_path) as catalog:
        (after,) = catalog.list_inbox_entries(None, limit=10)
        assert after.local_id == entry.local_id
        assert after.state is decided
        signals = catalog.list_signals(SignalKind.RELEASE, limit=10)
        assert len(signals) == 2
        (updated,) = (signal for signal in signals if signal != original_signal)
        assert after.latest_signal_local_id == updated.local_id
        stored = catalog.get_release(release_local_id)
        assert stored is not None
        assert updated.material_version == content_version_of(stored)
        assert updated.explanation.reasons[-1].kind is ExplanationReasonKind.UPDATED_RELEASE
        assert catalog.list_release_observations_pending(limit=10) == ()

    third = _run_refresh(
        "cli",
        "releases",
        catalog_path,
        lambda: httpx.MockTransport(handle),
        now=crashed_at + timedelta(days=2),
    )
    assert third["status"] == "succeeded"
    assert _metrics(third).get("signals_repaired", 0) == 0
    assert _metrics(third).get("signals_created", 0) == 0


@pytest.mark.parametrize("decided", [InboxState.DISMISSED, InboxState.SAVED])
def test_repair_of_a_decided_release_with_a_second_source_mints_nothing_through_cli(
    clock: FakeClock, tmp_path: Path, decided: InboxState
) -> None:
    """Issue #57 sibling: the repair pass re-observes a decided two-source release.

    The release gains a Deezer reference by a real cross-source merge, and an interrupted
    write left it pending, so the next refresh re-records it through the single write path.
    Provenance is not content: no signal is minted and the entry is untouched. Folding the
    source references back into the content digest makes this test fail.
    """
    catalog_path = tmp_path / "catalog.sqlite3"
    _seed(catalog_path, count=1)
    handle = one_release_group_handler()
    first = _run_refresh("cli", "releases", catalog_path, lambda: httpx.MockTransport(handle))
    assert first["status"] == "succeeded"
    application = MusicFriendApplication(Catalog.open(catalog_path))
    try:
        (entry,) = application.list_inbox_entries(None, limit=10)
        refresh_module.update_inbox_state(application, entry.local_id, decided, updated_at=NOW)
        (decided_entry,) = application.list_inbox_entries(None, limit=10)
    finally:
        application.close()
    merged = attach_a_second_source(catalog_path)
    with Catalog.open(catalog_path) as catalog:
        # The state a run killed between the content commit and the signal write leaves.
        catalog.set_release_observation_pending(merged.local_id, True)
        signals_before = catalog.list_signals(SignalKind.RELEASE, limit=10)

    second = _run_refresh(
        "cli",
        "releases",
        catalog_path,
        lambda: httpx.MockTransport(handle),
        now=NOW + timedelta(days=21),
    )

    metrics = _metrics(second)
    assert second["status"] == "succeeded"
    assert metrics.get("signals_repaired", 0) == 0
    assert metrics.get("signals_created", 0) == 0
    assert metrics.get("failures", 0) == 0
    with Catalog.open(catalog_path) as catalog:
        assert catalog.list_release_observations_pending(limit=10) == ()
        assert catalog.list_signals(SignalKind.RELEASE, limit=10) == signals_before
        (after,) = catalog.list_inbox_entries(None, limit=10)
        assert after == decided_entry
        stored = catalog.get_release(merged.local_id)
        assert stored is not None
        assert {reference.source for reference in stored.source_refs} == {"musicbrainz", "deezer"}
