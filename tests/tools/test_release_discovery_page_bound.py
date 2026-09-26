"""Issue #49: release discovery bounds pages on raw pages, not only on kept records."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from music_friend.domain import ReleaseDiscoveryStatus
from music_friend.providers import Page
from music_friend.store import Catalog
from music_friend.tools import MusicFriendApplication
from tests.tools.test_release_discovery import NOW, FakeReleaseSource, _artist, _watch


def test_release_discovery_bounds_pages_even_when_every_row_is_filtered_out(
    tmp_path: Path,
) -> None:
    """A source whose pages keep advancing but whose rows its own filters drop (empty
    normalized pages) stops at the page bound and resumes from its continuation on the
    next run, instead of paging indefinitely."""
    with Catalog.open(tmp_path / "catalog.sqlite3") as catalog:
        application = MusicFriendApplication(catalog)
        artist = _artist("one")
        _watch(application, artist)
        source = FakeReleaseSource()
        cursor: str | None = None
        for page in range(1, 50):
            source.pages[("one", cursor)] = Page((), str(page * 100))
            cursor = str(page * 100)

        result = application.discover_releases("spotify", source, checked_at=NOW)

        assert len(source.calls) == 5
        (artist_result,) = result.artists
        assert artist_result.status is ReleaseDiscoveryStatus.PARTIAL
        assert artist_result.records_seen == 0
        assert artist_result.continuation == "500"

        application.discover_releases("spotify", source, checked_at=NOW + timedelta(hours=1))

        assert [call[2] for call in source.calls[5:]] == ["500", "600", "700", "800", "900"]
