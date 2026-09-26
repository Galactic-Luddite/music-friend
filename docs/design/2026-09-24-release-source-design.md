# Release discovery off Spotify: MusicBrainz hub, Deezer timeliness layer

Status: approved design, 2026-09-24. Tracks issues #40 (catalog freshness TTL), #41
(MusicBrainz release source and identity mapping), #42 (Deezer timeliness layer).

## Problem

Spotify's Web API development mode is the only tier available to individuals: extended quota is
granted solely to registered companies with a launched product and at least 250,000 monthly active
users (policy since 2025-05-15), development-mode quota is counted per developer account across
all client IDs (since 2026-07), apps are capped at five users, and refresh tokens expire six months
after authorization. Measured on 2026-09-24 against a library of a few thousand saved items, a
`refresh all` run made about 70 requests in six minutes before a rolling-window 429 with a
two-minute cooldown; four consecutive runs never reached release discovery.

Spotify therefore cannot be the daily release-discovery source. It remains the only source of the
user's own library, which is small, changes slowly, and is exactly what development mode can serve
once #40 stops re-paginating it.

## Decision

Hub-and-spoke:

- **MusicBrainz** is the identity hub and the always-on release baseline. Keyless, 1 request per
  second per IP, stable, and its URL relationships map a Spotify artist to MusicBrainz, Deezer and
  Apple Music identifiers in one batch call.
- **Deezer** is the timeliness layer. Label-fed, so it carries long-tail releases on street date
  that MusicBrainz's volunteer editors add late (probe on 2026-09-24: 4 of 6 chart releases from
  the previous ten days were in MusicBrainz; the misses were an indie band and a game soundtrack).
  Its keyless catalog reads work today but new app registration has been frozen since 2025, so it
  is layered and feature-flagged, never load-bearing.
- **Apple iTunes Search API** is explicitly unsupported by Apple; not built.

Each piece ships independently and the tool works with any prefix of them delivered.

## Verified API facts

| Fact | Evidence |
| --- | --- |
| MusicBrainz release-group search accepts `arid:<MBID> AND firstreleasedate:[DATE TO *]` in one call, `limit` up to 100 | Live probe: 41 release groups for one artist since 2023-01-01, single request |
| Results are not date-sorted and include bootlegs and broadcasts | Same probe; client sorts and filters `status:official`, `primarytype:(Album OR Single OR EP)` |
| `/ws/2/url?resource=<url>&inc=artist-rels` takes up to 100 `resource` parameters | MusicBrainz API docs |
| Artist url-rels carry Spotify, Deezer and Apple Music links | Live probe on a public artist: 73 url-rels including all three |
| MusicBrainz rate limit 1 req/s per IP, meaningful `User-Agent` mandatory, 503 on violation | MusicBrainz rate-limiting docs |
| Deezer `/artist/{id}/albums` works without a key and returns `release_date` | Live probe 2026-09-24 |
| Deezer `/editorial/0/releases` returns zero rows | Live probe; do not depend on it |
| Spotify path currently requests `include_groups=album,single` | `providers/spotify/source.py` |

Full research with per-claim confidence is in the release-source research note kept with the
job that produced this design; the table above is the subset the implementation depends on.

## Architecture

### Existing seams (no change)

- `MusicSource` Protocol (`providers/base.py`): `capabilities()`, `health()`,
  `recent_releases(artist_refs, since, cursor) -> Page[Release]` and the catalog methods.
- `refresh_once()` (`tools/refresh.py`) receives sources explicitly; each source is wrapped in a
  `_PacedSource` whose limit state is loaded by `source_name` from `source_limits`.
- `discover_releases()` (`tools/release_discovery.py`) resolves the artist's `SourceReference`
  for `source_name`, applies `FRESHNESS_TTL`, and persists via `release_check_cursors`,
  `release_check_continuations`, `source_cursors`, all keyed by source name.
- `Artist.source_refs: tuple[SourceReference, ...]`; `Catalog.put_artist()` replaces the set.
- `release_discoveries` is unique on `(source, provider_native_id)` and has a variant lookup
  index on `(source, normalized_title, release_date)`.

### 1. Release-source seam (#41)

`refresh_once(..., source, source_name, release_source, release_source_name, ...)`.

- The `catalog` component uses `source` (Spotify). The `releases` component uses
  `release_source`, wrapped in its own `_PacedSource(source_name=release_source_name)`, so
  pacing, cooldowns, resume cursors and freshness cursors stay per source with no schema change.
- When `release_source_name == source_name` the same paced wrapper is shared (today's behaviour).
- `LocalConfig.release_source: Literal["spotify", "musicbrainz"] | None`; `None` means
  `musicbrainz`. Config document version 2 -> 3: a version-2 file loads with
  `release_source=None` and is rewritten as version 3 on next save. Unknown values are rejected.
- Surfaces: `setup --release-source spotify|musicbrainz`; `get_setup` returns it;
  `update_setup` accepts it; `doctor` reports `release_source` and, for `musicbrainz`, the
  `unmapped_artists` count with the remedy `music-friend refresh releases` (mapping runs there).
- CLI and MCP build the release source from config: `musicbrainz` needs no credentials.

### 2. Identity mapping (#41)

Runs at the start of the `releases` component when the release source is not Spotify.

1. Select watchlist artists with no `SourceReference` for the release source and no mapping
   attempt inside the retry window.
2. Batch up to 100 Spotify canonical URLs per `/ws/2/url?resource=...&inc=artist-rels` call.
   A URL whose relations include exactly one artist yields that MBID with
   `IdentityConfidence.EXTERNAL_ID`.
3. Remaining artists: `/ws/2/artist?query=artist:"<name>"&limit=3`. Accept only when the top hit
   has `score >= 90` and the second hit, if any, has `score < 90`. Confidence stays
   `SOURCE_ONLY`; the mapping row records `method=name_search`.
4. Otherwise the artist is `unmapped`.

Persistence:

- Mapped: `put_artist()` with existing refs plus
  `SourceReference("musicbrainz", <MBID>, "https://musicbrainz.org/artist/<MBID>", now)`.
- New table `artist_identity_mappings(artist_local_id, source, status, method, attempted_at,
  PRIMARY KEY (artist_local_id, source))`, `status in ('mapped', 'unmapped')`. Unmapped rows are
  retried after `MAPPING_RETRY_INTERVAL = 7 days`, derived as `7 * DAILY_REFRESH_MINUTES`.
- `update_watchlist` gains an optional `source_ids: {musicbrainz: <MBID>}` field that sets the
  reference with `USER_CONFIRMED` confidence and a `method=user` row. MBIDs are validated as
  UUIDs; the tool never fetches to validate.
- `music_status` adds `identity: {source, mapped, unmapped}` counts. `list_watchlist` entries
  carry `release_source_status: mapped|unmapped`.

Mapping requests are paced by the same MusicBrainz `_PacedSource` as discovery and count toward
its request budget. 500 artists cold-start at 5 batch calls plus at most 500 name searches, all
under 1 req/s: roughly nine minutes once, then zero.

### 3. `MusicBrainzSource` (#41)

Package `providers/musicbrainz/` mirroring `providers/spotify/`: `transport.py`,
`normalize.py`, `source.py`.

- Transport: base `https://musicbrainz.org/ws/2/`, `Accept: application/json`, `User-Agent:
  music-friend/<version> (https://github.com/Galactic-Luddite/music-friend)`. Minimum 1.0 s
  between requests enforced locally. 503 or 429 maps to `RateLimitedError(retry_after=...)`
  (default 5 s when no header). Response bodies are bounded and parsed strictly like Spotify's.
- `capabilities()`: supported and granted = `{RECENT_RELEASES}`; every other `MusicSource` method
  raises `CapabilityUnsupportedError`.
- `recent_releases(artist_refs, since, cursor)`: one artist per call (as Spotify today);
  query `arid:<MBID> AND firstreleasedate:[<since date> TO *] AND status:official AND
  primarytype:(Album OR Single OR EP)`, `limit=100`, `offset` from the cursor. Results are
  filtered to `score` present, sorted by first-release-date descending, and normalized.
- Normalizer: release-group -> `Release` with `local_id = sha256(domain, "musicbrainz",
  "release", <rgid>)`, `title`, `release_type = primary-type.lower()`, `release_date` and
  `date_precision` from `first-release-date` (`YYYY`, `YYYY-MM`, `YYYY-MM-DD`; empty date is
  rejected), `artist_refs` from `artist-credit` MBIDs mapped through the catalog's known
  MusicBrainz references, `source_refs = (SourceReference("musicbrainz", rgid,
  "https://musicbrainz.org/release-group/<rgid>", now),)`.
- Passes `tests/contracts/source_contract.py` with recorded synthetic fixtures.

### 4. Deezer timeliness layer (#42)

- `LocalConfig.release_sources: tuple[str, ...]` replaces the scalar from #41; default
  `("musicbrainz",)`; allowed `{"spotify", "musicbrainz", "deezer"}`; config version 4.
- `providers/deezer/`: keyless transport (`https://api.deezer.com/`), local pacing at 10
  requests per 5 s (half the community-reported ceiling), `quota_exceeded` error body ->
  `RateLimitedError`. `recent_releases` calls `/artist/{id}/albums?limit=100` and filters
  `release_date >= since` client-side; `record_type` maps to `release_type`.
- Deezer artist IDs come from the same MusicBrainz url-rels batch (relation
  `https://www.deezer.com/artist/<id>`), stored as `SourceReference("deezer", ...)`. No Deezer
  name search: an artist without a url-rel is simply not covered by Deezer.
- The `releases` component iterates configured sources in order, each with its own paced wrapper
  and cursors. A source that is rate-limited or unreachable does not block the next.
- Cross-source dedupe: `find_release_discovery_variant` gains a source-agnostic form matching
  `artist_local_id + normalized_title + release_date`, widened to plus or minus one day only when
  both dates have day precision. A month- or year-precision date never matches a day inside that
  period: a false merge hides a real release, which is worse than a duplicate. On a match the existing release gains the second
  `SourceReference`, `last_seen_at` updates, and no new inbox item is created. New index on
  `release_discoveries (normalized_title, release_date)`.

## Request budget

| Scenario | Spotify | MusicBrainz | Deezer |
| --- | --- | --- | --- |
| Daily, 50 artists, warm | 0 (catalog fresh per #40) | 50 | 50 |
| Daily, 500 artists, warm | 0 | 500 (about 9 min at 1 req/s) | 500 (about 4 min) |
| Cold start, 500 artists | catalog pages once | 5 batch + up to 500 searches + 500 | 500 |

## Error handling

- A `RateLimitedError` from any release source records a `source_limits` cooldown for that
  source and marks the run `partial` with `reason=rate_limited`, as today.
- Mapping failures for one artist never fail the run; the artist is `unmapped` and retried later.
- Invalid or oversized responses raise `InvalidSourceResponseError` and count as a failure for
  that artist only.
- Nothing in this design writes to any provider.

## Security and privacy

- No new secrets. MusicBrainz and Deezer are keyless; `doctor` gains no credential check.
- Outbound requests carry only public artist identifiers or names already in the local catalog.
- Fixtures, tests, issues and PRs use synthetic artists only.

## Testing

- Contract: `tests/providers/musicbrainz/test_source_contract.py` and
  `tests/providers/deezer/test_source_contract.py` run the shared source contract.
- Normalizers: date precision, missing date rejection, type filter, bounded body.
- Mapping: exact url-rel hit; ambiguous url-rel (two artists) falls to name search; name search
  accepted at 90 with a distant second; rejected when two hits are at or above 90; unmapped retry
  window honoured; user-supplied MBID wins.
- Refresh: with `release_source=musicbrainz`, a `releases` run makes zero Spotify requests
  (fake Spotify source asserts no calls); a MusicBrainz cooldown does not touch Spotify state.
- Dedupe (#42): the same release from MusicBrainz and Deezer yields one release, two source refs,
  one inbox item; a day-offset date still matches.
- Config: version 2 file loads and re-saves as version 3 (then 4); unknown source rejected.
- Regression: `MAPPING_RETRY_INTERVAL` and the Deezer pacing constants are asserted against the
  shared `DAILY_REFRESH_MINUTES`-derived bounds.

## Out of scope

Removing Spotify entirely (watchlist from import only), Apple or Bandcamp sources, ListenBrainz,
batch Spotify requests (#37), and any change to event discovery.
