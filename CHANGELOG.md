# Changelog

## Unreleased

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
