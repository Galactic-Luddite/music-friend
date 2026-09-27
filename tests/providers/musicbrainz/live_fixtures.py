"""Verbatim MusicBrainz responses recorded from the live API.

``fixtures/release_group_search_offset0.json`` is the unmodified JSON body of one
``GET /ws/2/release-group`` search (``limit=2``, ``offset=0``) recorded on 2026-09-26
with the Music Friend User-Agent. The query matches the one
``MusicBrainzSource.recent_releases`` builds, for MusicBrainz's public special-purpose
"Various Artists" entity (not anyone's library) with a first-release date on or after
2026-08-01. Tests may derive further pages from it but must not hand-write its shape.

``fixtures/release_browse_url_rels.json`` is the unmodified JSON body of one
``GET /ws/2/release?release-group=<rgid>&inc=url-rels&limit=100`` browse recorded on
2026-09-27 with the Music Friend User-Agent, for the first release group of the search above
(issue #63). Its ``free streaming`` relation points at a non-Spotify, non-Deezer store;
``release_browse_with_synthetic_streaming_links`` rewrites that recorded relation to
synthetic Spotify and Deezer album ids, keeping every other field as recorded.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

#: The public special-purpose MusicBrainz artist the recorded search was run for.
LIVE_SEARCH_ARTIST_MBID = "89ad4ac3-39f7-470e-963a-56509c546377"

_FIXTURES = Path(__file__).with_name("fixtures")

#: The release group the recorded browse was run for (the first search result above).
LIVE_BROWSE_RELEASE_GROUP_ID = "408df83e-a563-495b-ab65-9deea6c6f500"
#: Synthetic album ids written into the recorded ``free streaming`` relation.
SYNTHETIC_SPOTIFY_ALBUM_ID = "0SyntheticAlbumId00001"
SYNTHETIC_DEEZER_ALBUM_ID = "900000001"


def load_live_release_group_search() -> dict[str, Any]:
    """Return a fresh copy of the recorded release-group search response."""
    value = json.loads(
        (_FIXTURES / "release_group_search_offset0.json").read_text(encoding="utf-8")
    )
    assert isinstance(value, dict)
    return value


def load_live_release_browse() -> dict[str, Any]:
    """Return a fresh copy of the recorded release browse with url-rels, unmodified."""
    value = json.loads((_FIXTURES / "release_browse_url_rels.json").read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def release_browse_with_synthetic_streaming_links() -> dict[str, Any]:
    """The recorded browse with its ``free streaming`` relation rewritten to synthetic ids.

    The recorded relation is copied twice, once per synthetic album URL, so both links keep
    the exact recorded relation shape.
    """
    value = load_live_release_browse()
    for release in value["releases"]:
        rewritten: list[dict[str, Any]] = []
        for relation in release["relations"]:
            if relation["type"] != "free streaming":
                rewritten.append(relation)
                continue
            for url in (
                f"https://open.spotify.com/album/{SYNTHETIC_SPOTIFY_ALBUM_ID}",
                f"https://www.deezer.com/album/{SYNTHETIC_DEEZER_ALBUM_ID}",
            ):
                copy = json.loads(json.dumps(relation))
                copy["url"]["resource"] = url
                rewritten.append(copy)
        release["relations"] = rewritten
    return value
