# Provider limits and manual verification

Spotify is the current music provider. Catalog and release results depend on the connected
application’s permissions and the records Spotify returns. Release discovery looks back 30 days,
then overlaps the last successful refresh by 48 hours. Processing is limited to 100 releases per
artist; any remaining work is reported as partial rather than complete.

Music Friend spaces music-source requests across a rolling window and records request and
rate-limit pause counts in each refresh summary. When a source asks for a short pause, the refresh
may retry the same request within its ten-minute budget. Missing or invalid retry guidance uses a
bounded exponential estimate of 60, 120, 240, 480, and then 900 seconds. A refresh pauses at most
twice; longer waits, exhausted pause allowance, quota exhaustion, or an insufficient remaining
budget return a partial result.

Release refreshes remember an interrupted artist. A later refresh waits for an active cooldown to
expire before resuming. If the artist is no longer on the watchlist, discovery restarts with the
current watchlist. Completing the watchlist clears the interrupted state. Source-limit state is
included in exports and imports.

MusicBrainz is the default release source. It requires no API key; Music Friend spaces MusicBrainz
requests at no more than one request per second and records bounded partial outcomes if a refresh
cannot complete.

Deezer is an optional, feature-flagged release source layered on top of MusicBrainz for
timelier releases; enable it by adding `deezer` to `release_sources` (see docs/setup.md). It
requires no API key; Music Friend spaces Deezer requests at no more than 10 requests per 5 seconds.
Deezer artist identities come only from the MusicBrainz url-relationship lookup Music Friend
already performs for identity mapping -- there is no Deezer name search, so an artist without a
Deezer link on its MusicBrainz page is simply not covered by Deezer. When configured, `refresh_music`
tries each configured release source in order; a Deezer rate limit or outage does not block
MusicBrainz (or vice versa), and the refresh result reports which source, if any, was partial. The
same release discovered through two different sources becomes one release with both sources'
references attached, not two inbox items; a one-day difference in reported release date across
sources is treated as the same release, but a different title is not.

Ticketmaster is optional and does not establish a completeness guarantee for live events. Music
Friend searches only an explicit country and postal-code area. It uses a default radius of 50 miles
or 80 kilometers when a configured area omits a radius, searches the next 365 days, asks for up to
200 events per query, and spaces requests at no more than two per second. Exact music-attraction
matches are required; ambiguous names remain unmatched. Normalized event checks are cached for six
hours. Provider links and available attribution are retained, but Music Friend does not sell
tickets.

Provider terms, quotas, supported regions, redirect requirements, and API behavior can change.
Consult [Spotify developer documentation](https://developer.spotify.com/documentation/web-api) and
[Ticketmaster Discovery API documentation](https://developer.ticketmaster.com/products-and-docs/apis/discovery-api/v2/)
before relying on a provider result.

## Display-name sanitization

Every display name that enters Music Friend from an external source -- imported Spotify listening
history (track, artist, and album names), Spotify catalog/provider refresh (artist, track, and
release names), and Ticketmaster event discovery (event title, venue name, and locality) -- is
sanitized before it is stored. Sanitization removes Unicode bidirectional-control characters
(`U+202A`-`U+202E`, `U+2066`-`U+2069`) and every other Unicode control (`Cc`) and format (`Cf`)
character, so a name cannot visually reorder or hide text in a terminal or agent transcript.

Two format characters are kept rather than stripped: ZERO WIDTH JOINER (`U+200D`), required to keep
multi-codepoint emoji sequences (for example a skin-tone or gender modifier) rendering as one
glyph, and ZERO WIDTH NON-JOINER (`U+200C`), required by some scripts -- for example Persian and
several Indic scripts -- to select the correct glyph shape. Legitimate non-Latin text (Arabic,
Hebrew, CJK, Cyrillic, and others) is never altered; only control and format characters are
affected. The shared sanitizer is `music_friend.domain.text.sanitize_display_name`.

Rows written before this sanitization existed are cleaned lazily: every display name an MCP tool
returns is sanitized again at that read boundary, so old data never needs a database migration to
become clean. See [MCP clients](mcp.md#tool-surface) for the MCP tool surface this applies to.

## Manual provider check

After creating a test provider application and using a non-sensitive test account, verify locally:

1. `music-friend connect spotify` completes without a client secret.
2. `music-friend refresh catalog --json` reports the actual status and does not echo protected data.
3. `music-friend refresh releases --json` reports a result or a bounded partial outcome.
4. If optional event enrollment becomes available, `music-friend refresh events --json` uses only
   the chosen home area and provides discovery links without a purchase action.
5. `music-friend diagnostics --json`, exports, and backups contain no provider credential.
6. `music-friend disconnect spotify` removes the local Spotify credential.

Do not attach provider responses, authorization redirects, exports, databases, or diagnostics that
contain personal data to an issue or support request.
