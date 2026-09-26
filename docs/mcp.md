# MCP clients

Use the CLI to complete setup before connecting an MCP client. The local stdio server is:

```bash
music-friend-mcp
```

MCP handles local music, watchlist, inbox, and listening-history requests. It does not accept or
manage credentials, schedules, imports, restores, backups, or full-data deletion. The
encrypted-vault fallback is interactive CLI only; the current stdio MCP server requires an
approved native credential store.

## How it runs

Music Friend is built to be used by an AI assistant, not through its own interface. Your MCP client
(Claude Code, Claude Desktop, Codex, or another stdio client) starts `music-friend-mcp` as a child
process on the same computer when a conversation needs it, calls its tools, and stops it when the
session ends. There is no resident service, port, or hosted component: the assistant reads the
local catalog directly, and only `refresh_music` reaches out to Spotify, MusicBrainz, Deezer, or
Ticketmaster, read-only.

Because the server runs on your computer, register it on the machine that holds your Music Friend
data and credentials. Run `music-friend doctor` there first; the server needs an approved native
credential store.

## Interface boundary

MCP schemas are the universal integration interface. A compatible client discovers the advertised
tools and their schemas from the local server; it does not need provider-specific instructions.

## Tool surface

Music Friend exposes exactly these eleven tools. Every tool works with local identifiers and bounded
arguments, never raw provider identifiers. "Local write" means the tool changes only your local
catalog; "provider contact" means it makes read-only network requests to Spotify, MusicBrainz,
Deezer, or Ticketmaster.

| Tool | What it does | Important inputs | Result or effect | Behavior |
|------|--------------|------------------|------------------|----------|
| `music_status` | Quick overview: is the catalog ready, is there unread inbox, when was the last refresh, is the source ready or cooling down | none | `status`, `inbox.has_unread`, `latest_refresh`, `source_limits.spotify` (`ready`, `state`, `retry_at`), and MusicBrainz `identity` mapping counts when configured | read-only, local |
| `refresh_music` | Run one bounded refresh and write results to the local catalog | `kind`: `catalog`, `releases`, `events`, or `all`; `force` (boolean, default `false`) | a refresh run summary, `partial` when interrupted or already running, or `skipped` with `reason: "event_area_not_configured"` when `kind: "events"` runs with no event area set | local write, provider contact |
| `search_catalog` | Find local artists by name, usually before a watchlist change | `query` (1-256 chars), `limit` (1-50) | `items`: matching artists with local identifiers | read-only, local |
| `list_watchlist` | Show monitored artists and why each is included | `limit` (1-100) | `items`: watchlist entries; entries include `release_source_status` (`mapped` or `unmapped`) for the first configured identity-mapped source (MusicBrainz or Deezer) | read-only, local |
| `update_watchlist` | Record an explicit watchlist decision for one artist | `artist_id` (local), `action`: `add`, `pin`, `mute`, or `remove`; optional `source_ids` mapping source names to user-confirmed identifiers | the applied decision, or `not_found` | local write |
| `list_inbox` | Show release and event items, optionally filtered by state | `state`: `unread`, `saved`, `dismissed`, or null; `limit` (1-100) | `items`: inbox entries, each with a `summary` (`kind`, `title`, `artist_names`, `date`) so most requests need no follow-up call | read-only, local |
| `update_inbox_item` | Set one inbox item to `unread`, `saved`, or `dismissed` | `inbox_id` (local), `state` | the updated entry (including its `summary`), or `not_found` | local write |
| `explain_inbox_item` | Show why an item appeared, with its release or event record | `inbox_id` (local) | `entry` (with `summary`), `record` (with `artist_names` alongside `artist_ids`), and `reasons` (why it was included) | read-only, local |
| `summarize_listening_history` | Summarize imported plays for a UTC date range | `since`, `until` (RFC 3339 with an offset or `Z`, or null), `limit` (1-50) | evidence boundary, covered dates, play time, brief and skipped counts, top artists and tracks | read-only, local |
| `get_setup` | Query Music Friend configuration state and completion status | none | configured fields (Spotify ID, event area fields, `release_sources`), which fields are missing, whether setup is complete for MCP readiness | read-only, local |
| `update_setup` | Update Music Friend configuration fields (non-secrets only) | `client_id`, `event_country_code`, `event_postal_code`, `event_radius`, `event_radius_unit`, `release_sources` (an ordered array chosen from `spotify`, `musicbrainz`, `deezer`; all optional, pass null to leave unchanged) | updated configuration state, or instructions to use CLI for secrets | local write |

A tool returns a `category` of `invalid_arguments`, `not_found`, or `internal_error` instead of a
result when it cannot complete the request. Error messages are redacted and never include provider
responses. An `invalid_arguments` message names the offending argument and the constraint it
violated (for example, `"limit must be an integer from 1 through 50"` or `"action must be one of:
add, pin, mute, remove"`) but never echoes the caller-supplied value back, so a malformed or
sensitive-looking value never appears in the response. `internal_error` is reserved for failures
that are not the caller's mistake (an unexpected local failure decoding stored data, for example);
its message is always the same generic sentence and never includes exception text.

Every `local_id`-shaped argument (`artist_id`, `inbox_id`) is a value returned by another tool, not
something to invent: `artist_id` comes from `search_catalog` or a `list_watchlist` entry's
`artist.local_id`; `inbox_id` comes from `list_inbox` or the `entry.local_id` in
`explain_inbox_item`'s result. Every display name a tool returns -- artist, release, event, venue,
and imported-history names -- has bidirectional-override and other control characters removed; see
[display-name sanitization](limits.md#display-name-sanitization) for the allowlist and rationale.

A `partial` `refresh_music` result adds `reason` (`rate_limited`, `quota_exhausted`, or
`deadline`), `retry_after` (an ISO-8601 timestamp, only present for `rate_limited`), and
`remaining` (how many records this run skipped) when the cause is known. Call `music_status`
first, or read `retry_after` from a prior `partial` result, before starting another refresh during
a cooldown -- see [adaptive request pacing](operations.md#adaptive-request-pacing).

`refresh_music` with `kind: "catalog"` or `"all"` skips a catalog capability (followed artists,
saved items, each top-items time range) that completed successfully within the freshness window --
see [catalog freshness](operations.md#inspect-and-refresh) -- making zero source requests for it
and reporting it `skipped_fresh`. A failed capability is never marked fresh and is retried in full
on the next run. Pass `force: true` to bypass the skip and re-paginate every catalog capability
regardless of freshness. The result's metrics include `catalog_skipped_fresh`, the number of
catalog capabilities skipped as fresh in that run; `music_status` reports the same count from the
latest run.

`refresh_music` with `kind: "events"` and no event area configured makes no source requests; it
returns `{"status": "skipped", "reason": "event_area_not_configured"}` instead of reporting
`succeeded`, so a caller never tells the person "no nearby events" when nothing was actually
checked. `refresh_music` with `kind: "all"` still runs catalog and release discovery normally and
reports the run's real `status`; when the event area is unconfigured it adds
`events_skipped_reason: "event_area_not_configured"` to the result so the events portion of the run
is identifiable without failing the rest of the refresh.

### `search_catalog` matching rule

Matching is case- and accent-insensitive: the query and every stored artist name are folded
through a Unicode NFKD decomposition with combining marks stripped (so `elodie cafe` finds an
artist stored as `Élodie Café`, and a plain `Y` finds a name stylized with `Ÿ`), then case-folded.
`-` and `&` are treated as separators (collapsed to a space alongside surrounding whitespace) so
punctuation does not block a reasonable match, e.g. `rock n roll` finds `Rock-N-Roll`. Only the
comparison is folded; `display_name` in the result is always the stored name, unchanged.

### Follow an artist through the inbox

A synthetic example of a normal conversation. Identifiers are placeholders.

1. `search_catalog` with `query: "Example Quartet"`, `limit: 5` returns one artist with
   `local_id: "art_0001"`; pass that value as `artist_id` to the next call.
2. `update_watchlist` with `artist_id: "art_0001"`, `action: "pin"` records the decision.
3. `refresh_music` with `kind: "releases"` reads Spotify and writes new releases to the inbox.
   Use `kind: "events"` after an event area is configured to look for nearby shows.
4. `list_inbox` with `state: "unread"`, `limit: 20` returns an item with `local_id: "inb_0042"`;
   pass that value as `inbox_id` to the next calls.
5. `explain_inbox_item` with `inbox_id: "inb_0042"` shows that the release matched a pinned
   artist.
6. `update_inbox_item` with `inbox_id: "inb_0042"`, `state: "saved"` keeps it for later.

The server uses stdio only. It has no network listener.

## Codex

Add this local server configuration to `~/.codex/config.toml`:

```toml
[mcp_servers.music-friend]
command = "music-friend-mcp"
```

## Claude Code

Register the server from a terminal:

```bash
claude mcp add music-friend -- music-friend-mcp
```

Or add the equivalent entry to a Claude Code or Claude Desktop MCP configuration:

```json
{
  "mcpServers": {
    "music-friend": {
      "command": "music-friend-mcp",
      "args": []
    }
  }
}
```

If you installed into a virtual environment, use the absolute path to `music-friend-mcp` inside
that environment's `bin` directory, because MCP clients do not inherit your shell's activated
environment.

## Other stdio clients

A compatible client starts `music-friend-mcp`, completes normal MCP tool discovery, and calls only
the advertised tools.

## vLLM and Ollama

vLLM and Ollama can be used through a client that supports an OpenAI-compatible function-call loop.
Compatibility depends on that client and its configuration.

## Optional runtime skill

The canonical optional runtime skill remains at `skills/music-friend/SKILL.md` in a source
checkout and is packaged unchanged in the wheel. It can be installed from the wheel for Codex,
Claude, or a custom skills directory:

```bash
music-friend skill install --client codex
music-friend skill install --client claude
music-friend skill install --target SKILLS_DIRECTORY
```

See the [install guide](install.md#optional-agent-skill) for destinations, replacement behavior,
and local safety boundaries. MCP schemas remain sufficient for clients that do not load skills;
the skill is optional convenience guidance.

## Contributor guidance

Repository contributor guidance applies only inside a source checkout. It describes source layout,
validation, synthetic-data requirements, and provider-neutral architecture constraints; it is not
a runtime discovery mechanism.

## Listening-history evidence

`summarize_listening_history` answers bounded historical questions from locally imported Spotify
music plays. Import the archive first with the CLI (see
[CLI and data operations](operations.md#import-spotify-listening-history)); MCP cannot import.

Supply nullable `since` and `until` timestamps and a ranking limit from 1 through 50. `since` and
`until` accept any RFC 3339 date-time with an explicit UTC offset -- `Z`, or a positive or negative
`±HH:MM` offset -- and are normalized to UTC before the catalog is queried; the result's `since`
and `until` are always reported back in UTC (`Z` suffix). A naive timestamp (no offset and no `Z`)
is rejected as `invalid_arguments`, with a message stating that an offset or `Z` is required. A
reversed range (`since` after `until`), a zero-length range (`since` equal to `until`), and an
impossible calendar date (e.g. `2026-02-30T00:00:00Z`) are each rejected as `invalid_arguments`
with a message naming the specific problem; a zero-length range is treated as a caller error rather
than an empty summary, for the same reason a reversed range is: it almost certainly was not the
caller's intent, and a loud error is more useful than a silent empty result. Every caller-facing
range/timestamp/limit problem is reported as `invalid_arguments`; an internal failure unrelated to
the caller's arguments (for example, a corrupt stored row) is reported as `internal_error` instead
of being misreported as the caller's mistake. The result identifies its evidence boundary, covered dates,
play time, brief/skipped counts, and top artists and tracks. Imported plays remain separate from
preferences and watchlist affinity.

A synthetic example: "What did I listen to most in March 2024?" becomes
`summarize_listening_history` with `since: "2024-03-01T00:00:00Z"`,
`until: "2024-04-01T00:00:00Z"`, `limit: 10`. The answer reports the plays covered by that range,
names the evidence boundary (`imported Spotify music history`), and does not treat play counts as
approved preferences.
