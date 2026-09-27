# Changelog

## [Unreleased]

- Fixed cross-source dedupe missing titles that differ only in typographic punctuation or Unicode
  form (issue #56): a curly apostrophe/quote, an en/em dash, an NFD-decomposed accent, or doubled
  whitespace no longer produces a second inbox item for a release already reported under the ASCII
  or NFC-normalized spelling by another source. Only the comparison key is normalized; the stored
  display title is unchanged, and titles differing by a real character (for example "Stop" vs.
  "Stops", or a genuinely different accented letter) still stay separate. Added
  `music-friend data dedupe-inbox [--apply]` to find and, on request, dismiss existing inbox rows
  created before this fix that the same normalized key now treats as duplicates; dry-run by
  default, and dismissal is reversible (it marks the row `dismissed`, the same state
  `update_inbox_item` uses, rather than deleting it).
- Fixed `doctor` reporting only the first configured release source (`release_sources[0]`) instead
  of every source when two or more are configured; the existing `sources` list already reported
  them all correctly, so the misleading singular `source` key is dropped rather than duplicated.
- Fixed `update_setup`'s `event_radius` MCP argument silently accepting `true`/`false` as a valid
  radius (Python's `bool` is a subclass of `int`, so the prior `isinstance` check let a boolean
  through and stored it as radius `1`). `event_radius` now uses the same strict `type(value)`
  check as every other MCP argument validator, rejecting `true`, `false`, strings, and
  out-of-range numbers with `invalid_arguments`; `null` and numeric radii from `1` through `100`
  are still accepted. `LocalConfig`'s own radius validation was already strict.
- MusicBrainz's rate-limit pause budget is no longer shared with Spotify's two-pause cap: a
  MusicBrainz `503` is transient load shedding by MusicBrainz's own documentation, so a releases
  refresh now keeps pausing and retrying at the steady one request per second (honoring an exact
  `Retry-After` when present) until either the whole watchlist finishes or the ten-minute refresh
  deadline arrives, instead of giving up as `partial`/`rate_limited` after two pauses. Spotify's own
  pause budget and adaptive per-window learning are unchanged. Mapping and release discovery for
  MusicBrainz already shared one pacing state per run; a new test makes that explicit.
- Fixed a soundtrack/compilation title mismatch that produced duplicate inbox items: a trailing
  `(from ...)` or `[from ...]` attribution tag (for example `Song Z (from Some Film: The Album)`) is
  now folded like the existing `- Single` suffix and `(feat. ...)` credit list, so the same release
  reported with and without that tag by two different sources merges into one release with two
  source references. A deluxe/anniversary edition, a live version, and an acoustic version are
  deliberately left untouched and still stay distinct.
- Fixed duplicate inbox items for the same real-world release: a release re-seen by the same
  source on a later refresh no longer creates a second signal/inbox item (the missing-signal
  repair pass could mis-detect an already-signalled, unchanged release as missing its signal and
  re-record it under a different explanation, defeating dedup). Cross-source release matching now
  compares the full set of credited artists (any shared artist is enough, not only an exact
  primary-artist match) and folds conservative title decorations -- a trailing `- Single` suffix,
  a `(feat. ...)` credit list, and a generic `(Remix)` qualifier matched against a specific named
  `(<artist> Remix)` on the other side -- so a remix pair with a slightly different title and
  partially overlapping credits merges into one release with two source references. A deluxe or
  anniversary edition, two differently-named remixes, and the same title from unrelated artists
  still stay distinct. A generic `(Remix)` qualifier that could equally match two or more already-
  stored, differently-named remixes is ambiguous and is never merged with an arbitrarily chosen
  one; it stays its own separate release instead. Upgrading a real catalog's existing signals
  (written before this fix, in the older `material_version` format) is safe: the repair pass
  recognizes those older signals as already covering a release's current content and does not
  re-record them as duplicates, and never touches their existing inbox state.
- MusicBrainz release refreshes run at MusicBrainz's documented steady one request per second
  instead of the Spotify-tuned adaptive window: a `503` during identity mapping no longer ratchets
  the run down to one request per 30 seconds, and a limit stored by an earlier version no longer
  slows later runs. A normal daily refresh of a 50-artist watchlist now finishes well inside the
  deadline.
- The ten-minute refresh deadline is measured on a clock that keeps counting while the computer
  sleeps, so a refresh on a laptop that suspends mid-run can no longer run on far past it.
- MusicBrainz release discovery reads the search result total from `count` (the key the live
  `/ws/2/release-group` search returns), so an artist with more than 100 matching release groups is
  paged instead of silently truncated. Discovery reads at most five pages per artist per run even
  when every row on a page is filtered out, and resumes from its continuation on the next run.
- Every `partial` refresh result carries a `reason`: `rate_limited`, `quota_exhausted`, `deadline`,
  `source_errors`, or `already_running` (a run blocked by another refresh's lock, with
  `retry_after` set to when that lock goes stale).
- The `source_requests` metric now counts every provider request in the run, including
  MusicBrainz identity-mapping requests, additional release sources, and Ticketmaster; signals
  repaired for an earlier interrupted run are reported as `signals_repaired` instead of being
  folded into `signals_created`.
- Added a `release_source_unmapped` metric: the count of watchlisted artists a release refresh
  skipped this run for lack of a `SourceReference` on the release source being run. Surfaced in
  the `refresh_music` result and in `music_status.latest_refresh.metrics`, so a run that mapped
  nobody no longer reports a silent `succeeded` with no signal.

- Added MusicBrainz as the default release source; use `--release-sources` to configure it.
- `release_source` is now `release_sources`, an ordered tuple of `spotify`, `musicbrainz`, and/or
  `deezer` (config version 3 -> 4; a version-3 file's scalar `release_source` migrates to a
  one-element tuple, or the `musicbrainz` default when it was null). Added Deezer as an optional,
  feature-flagged release-timeliness source layered on top of MusicBrainz: keyless, paced at 10
  requests / 5 seconds, with artist identities resolved only from the MusicBrainz url-relationship
  lookup (no Deezer name search). `refresh_music` now iterates every configured release source in
  order with independent pacing, so a Deezer rate limit or outage never blocks MusicBrainz results,
  and the refresh result reports which additional source, if any, was partial. Cross-source release
  dedupe: the same release discovered via two different sources (matching artist, normalized title,
  and release date within one day) becomes one release with both sources' references attached
  instead of a duplicate release and inbox item. `--release-source` (singular) and the MCP
  `update_setup`/`get_setup` `release_source` field are replaced by `--release-sources` and
  `release_sources` respectively.

- Catalog sync (`refresh catalog` / `refresh all`, and the MCP `refresh_music` tool) now skips a
  capability (followed artists, saved items, each top-items time range) that completed
  successfully within the same freshness window release discovery already uses, making zero
  Spotify requests for it and reporting it `skipped_fresh`. A capability that ends `failed` is
  never marked fresh, so the next run retries it in full. This lets a daily refresh reach release
  discovery instead of exhausting the request budget re-paginating an unchanged library.
- New CLI flag `music-friend refresh catalog --force` and MCP `refresh_music` argument
  `force: bool` (default `false`) bypass the freshness skip for a deliberate full re-sync.
- The refresh result's metrics and `music_status` now report `catalog_skipped_fresh`, the number
  of catalog capabilities skipped as fresh in the latest run.

## 0.4.0

Minor release: setup, data commands, and Spotify connection can be driven by an agent without a terminal, two MCP configuration tools are added, and refresh paces itself under Spotify rate limits.

- `music-friend setup` now accepts non-interactive flags: `--spotify-client-id`, `--event-country`, `--event-postal`, `--event-radius`, `--event-unit`, and `--clear-<field>` variants for each field. Omitted flags preserve existing configuration. Setup works without a TTY when flags are provided.
- Ticketmaster API key can be supplied through `--ticketmaster-key-env VAR`, `--ticketmaster-key-file PATH` (enforces chmod 600), or `--ticketmaster-key-stdin`. The key never appears in stdout, stderr, JSON output, exception text, or logs.
- New MCP tools: `get_setup` (reports configuration state and completion status) and `update_setup` (updates non-secret fields). Secret values are never returned from MCP tools; the `update_setup` response directs users to the CLI for Ticketmaster key changes.
- `data delete` and `data restore` now accept `--yes` for automatic confirmation or `--confirm <token>` for token-based confirmation (expected tokens are "DELETE" and "RESTORE" respectively). These commands refuse to proceed without a TTY and without a confirmation flag.
- `music-friend connect spotify` supports `--json` flag for structured output.
- `music-friend doctor` remedies are now concrete, non-interactive command strings (e.g., `"music-friend setup --spotify-client-id example-client-id"`) instead of placeholder text.
- Backward compatibility: all interactive workflows remain unchanged when flags are not provided.
- Adaptive request pacing: each source's per-window request rate is now learned with an AIMD
  policy (halved on a 429, grown back gradually after a streak of successes) and persisted per
  source, so a large library's full refresh needs fewer runs and rarely hits a 429. Estimated
  fallback backoff delays are now jittered within their ladder step.
- A run started during a recorded cooldown waits or skips instead of contacting the provider;
  `status --json` and the MCP `music_status` tool now report a `source_limits.spotify` block
  (`ready`, `state`, `retry_at`) so a caller knows when the source will be ready again.
- A `partial` refresh result (CLI text/JSON and the MCP `refresh_music` tool) now includes
  `reason` (`rate_limited`, `quota_exhausted`, or `deadline`), `retry_after` (ISO-8601, when
  known), and `remaining` (records skipped this run).
- An artist whose releases were successfully checked within the last 20 hours is skipped on a
  later release refresh -- no source request at all -- cutting requests per artist on a repeated
  full run. The 20-hour window stays below the 24-hour scheduled refresh cadence so a scheduled
  run that starts slightly early is never mistaken for a repeat and does not skip a whole cycle.

## 0.3.0

Minor release: new, more specific error messages and CLI failure handling, richer MCP tool
descriptions, and display-name sanitizing change what clients see.


- Every MCP tool description now states its purpose, when to use it, what to call before and
  after it, and whether it contacts a provider; every `artist_id`/`inbox_id` argument now says
  which tool produces the value. Input schemas (types, required fields, enums) are unchanged.
- Fixes a bug where bidirectional-override and other Unicode control/format characters (for
  example `U+202E RIGHT-TO-LEFT OVERRIDE`) in imported history, catalog/provider refresh, release,
  and event names survived into stored data and MCP results unchanged. They are now removed at
  ingestion by a shared sanitizer (`music_friend.domain.text.sanitize_display_name`) that keeps
  ZERO WIDTH JOINER and ZERO WIDTH NON-JOINER so emoji sequences and scripts that need them are
  unaffected; see [display-name sanitization](docs/limits.md#display-name-sanitization). Names
  written before this fix are sanitized lazily wherever MCP returns them, so no migration is
  required.
- `summarize_listening_history` no longer misreports an internal failure (for example, decoding a
  corrupt stored row) as the caller's mistake: the history store now raises a dedicated
  `HistoryArgumentError` (a `ValueError` subclass) for caller-supplied `since`/`until`/`limit`
  problems, and the MCP handler translates only that type to `invalid_arguments`; every other
  exception is reported as `internal_error` with no exception text echoed to the client.
- MCP `invalid_arguments` messages now name the offending argument and the violated constraint
  (for example, `"limit must be an integer from 1 through 50"`) instead of the generic "Invalid
  tool arguments." sentence, and never echo the caller-supplied value.
- CLI `data export|backup|import|import-spotify|restore` now distinguish file-not-found,
  destination-already-exists, and invalid-archive failures with specific, actionable messages
  instead of one generic sentence. `data restore`/`data delete` report a distinct message when
  stdin is not an interactive terminal (so the confirmation prompt cannot be read at all), rather
  than the generic failure. `connect spotify`, `disconnect spotify`, and `refresh` now name
  "Spotify is not configured" and point at `music-friend doctor` instead of a generic failure.
  Existing CLI exit codes and message contracts are unchanged for cases documented before this
  release.

## 0.2.0

Minor release: additive MCP result fields and a new `skipped` refresh outcome change what clients
see, so this is a minor rather than a patch version.


- `search_catalog` now matches case- and accent/stylization-insensitively: the query and stored
  artist names are folded (Unicode NFKD, combining marks stripped, case-folded, `-`/`&` treated as
  separators) before comparison, so a plain-ASCII query finds an accented or stylized stored name.
  Display names in results are unchanged; matching runs in Python over the catalog rather than
  through SQLite `LIKE`, which cannot express the fold.
- `list_inbox` items now include a compact `summary` (`kind`, `title`, `artist_names`, `date`), so
  most "anything new?" requests no longer need a follow-up `explain_inbox_item` call per item.
  `explain_inbox_item`'s `record` now includes `artist_names` alongside `artist_ids`. Both changes
  are additive; no existing field was removed or renamed.
- Fixes `summarize_listening_history` to accept any RFC 3339 `since`/`until` timestamp with a
  positive or negative UTC offset (previously only the `Z` suffix was accepted), normalizing to
  UTC before querying. A naive timestamp (no offset and no `Z`) is still rejected, now with a
  message stating that an offset or `Z` is required.
- Fixes `summarize_listening_history` to return `invalid_arguments`, instead of `internal_error`,
  for a reversed range (`since` after `until`), a zero-length range (`since` equal to `until`), and
  an impossible calendar date (e.g. `2026-02-30T00:00:00Z`), each with a message naming the
  specific problem. A zero-length range is treated as a caller error, not an empty summary. No
  caller-supplied value to `summarize_listening_history` can produce `internal_error`.
- Fixes a refresh run's `finished_at` being copied from `started_at`, so every refresh reported
  zero duration. `finished_at` is now read from the clock again when the run actually completes.
- Fixes `diagnostics` reporting a source as `cooling_down` forever once a rate limit was observed,
  even after its `retry_at` had passed. The reported `state` now reflects the live cooldown as of
  the diagnostics call; the stored observation and its `consecutive_limits` history are unchanged.
- Fixes `refresh events` (CLI and the `refresh_music` MCP tool) reporting `succeeded` with zero
  source requests when no event area is configured. It now returns
  `{"status": "skipped", "reason": "event_area_not_configured"}` and makes no provider request.
  `refresh all` with the same missing configuration still refreshes catalog and releases normally
  and adds `events_skipped_reason: "event_area_not_configured"` to its result instead of masking
  the events portion as a plain success.

## 0.1.1

- Repositions the README and MCP guide around AI-agent-first use: what runs on your computer, how the MCP client starts `music-friend-mcp`, what you need before starting, and how to register the server with Claude Code and Codex.
- Provides explicit daily per-user refresh scheduling while keeping installation and setup free of scheduler side effects.
- Runs scheduled refreshes with the exact Python runtime used to install Music Friend.
- Reports installed and active scheduler state without opening the local music catalog.
- Makes repeated schedule installation update the existing per-user job.

## 0.1.0

- First standalone release of the local-first Music Friend CLI and MCP interface.
- Includes local catalog, watchlist, and inbox workflows with Spotify release discovery and optional Ticketmaster event discovery.
- Imports Spotify extended streaming-history archives through the local CLI (`data import-spotify`, with `--dry-run` validation) and answers bounded history questions through the `summarize_listening_history` MCP tool. Music-track plays only; IP address, device, and connection-country fields are never stored.
- Provides portable export and backup controls, documented privacy boundaries, and no hosted account or background service.
- Adds `music-friend doctor`, a local onboarding preflight that reports every missing prerequisite with its fix, and an `mcp_ready` field in `status` so a host without a native credential store is no longer reported as fully ready.
- Documents the release around user outcomes: a README with a "How it works" flow, a goal-oriented documentation index, a per-tool MCP reference covering all nine tools, a CLI guide organized by command group, a contributor guide that matches CI, and documentation contract tests.
