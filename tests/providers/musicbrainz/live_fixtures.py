"""Verbatim MusicBrainz responses recorded from the live API.

``fixtures/release_group_search_offset0.json`` is the unmodified JSON body of one
``GET /ws/2/release-group`` search (``limit=2``, ``offset=0``) recorded on 2026-09-26
with the Music Friend User-Agent. The query matches the one
``MusicBrainzSource.recent_releases`` builds, for MusicBrainz's public special-purpose
"Various Artists" entity (not anyone's library) with a first-release date on or after
2026-08-01. Tests may derive further pages from it but must not hand-write its shape.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

#: The public special-purpose MusicBrainz artist the recorded search was run for.
LIVE_SEARCH_ARTIST_MBID = "89ad4ac3-39f7-470e-963a-56509c546377"

_FIXTURES = Path(__file__).with_name("fixtures")


def load_live_release_group_search() -> dict[str, Any]:
    """Return a fresh copy of the recorded release-group search response."""
    value = json.loads(
        (_FIXTURES / "release_group_search_offset0.json").read_text(encoding="utf-8")
    )
    assert isinstance(value, dict)
    return value
