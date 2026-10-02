---
name: music-friend
description: Use when handling local music discovery, watchlists, release or event updates, inbox decisions, or Music Friend requests through an MCP client.
---

# Music Friend

Use Music Friend’s local MCP server for ordinary music requests. Work from the
local catalog and inbox; do not treat a provider as the conversation boundary.

## Response style

For a normal request, state the useful result first. Mention freshness only when it affects the
answer, and say that a refresh is partial only when its result is partial. Offer at most one useful
next action.

Do not narrate tool selection, internal work, checks, routing, or self-review. Do not add a
signature, status footer, or advice to retry unless the local result specifically calls for it.

## Normal requests

- For a quick overview, call `music_status`. Its `refresh.running` flag reports whether a
  refresh is in progress right now; other tools keep answering from local state while it runs.
  A non-zero `identity.conflicts` means some releases a source linked were kept as separate
  items; tell the person they can review them with `music-friend data inbox duplicates`.
- To update local information, ask for confirmation and then call `refresh_music` with one of
  `catalog`, `history`, `releases`, `events`, or `all`. Use `kind: "history"` when only recent
  listening observations are needed; it does not run catalog, release, or event work. A
  `kind: "events"` call with no event area configured
  returns `{"status": "skipped", "reason": "event_area_not_configured"}` and makes no provider
  request; report that no event area is set rather than "no nearby events". A `kind: "all"` call
  with the same missing configuration still runs catalog and release discovery and adds
  `events_skipped_reason: "event_area_not_configured"` to its result. A second `refresh_music`
  call while one is already running returns `{"status": "partial", "reason": "already_running"}`
  with a `retry_after` timestamp; wait and retry rather than repeating the call immediately.
- To find a known artist, call `search_catalog` before changing a watchlist. Matching is
  case- and accent-insensitive, so a plain-ASCII spelling still finds a stylized or accented
  stored name.
- To inspect monitored artists, call `list_watchlist`. Use `update_watchlist` only with a returned
  local artist identifier and an explicit add, pin, mute, or remove decision.
- To inspect updates, call `list_inbox`. Each item includes a compact `summary` (kind, title,
  artist names, date), so answering "anything new?" usually needs no follow-up call. Explain one
  item in more detail with `explain_inbox_item`. Its local states are `unread`, `saved`, or
  `dismissed`; change the state only through `update_inbox_item` after the person says what they
  want. An explicit `save it` authorizes setting the state to `saved` once the item identity is
  known and it has been explained.
- For historical listening questions, call `summarize_listening_history` with an explicit UTC
  range when the person names one. State the returned evidence boundary and do not reinterpret
  play counts as approved preferences.
  Archive `play_count` and listening time retain their original meaning. API observations have
  unknown duration, skip, and completion; describe combined counts as potentially duplicated or
  undercounted even when exact candidate overlap is zero. Use `api_top_artists` and
  `api_top_tracks` only as source-specific observation rankings.

Summarize results as local information. Event links are for discovery; never purchase tickets or
complete a transaction.

## Setup and sensitive work

Never ask for, accept, repeat, or place credentials in this conversation. Do not use MCP tools for
connection, credential storage, schedules, import, restore, backup, or deletion. Direct the person
to the local CLI and the setup and operations guides for those tasks.

Call `get_setup` to check which non-secret configuration fields are set and what's missing; it
never returns a secret value. Use `update_setup` only for the non-secret fields it reports
missing (event area and the developer client ID); the Ticketmaster key is always set through the
CLI, and `update_setup`'s response names the exact command to run for it.

Spotify extended-history archives are imported only through the local CLI's `data import-spotify`
command. Do not claim that a model runtime is an MCP client merely because it can use OpenAI-compatible tool
calls. A host performs the tool-call bridge.

`music_status.history` and `refresh_music.history` contain the allowlisted ongoing-sync outcome,
freshness, and coverage facts. They never expose provider URIs or cursors. A missing recently
played grant requires an explicit CLI reconnect; do not attempt connection or token repair through
MCP. Disconnect retains local history evidence, while the documented purge/delete commands remove it.
