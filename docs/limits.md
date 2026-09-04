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
