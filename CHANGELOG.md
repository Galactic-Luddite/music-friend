# Changelog

## [Unreleased]

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
