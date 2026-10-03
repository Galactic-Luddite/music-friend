# Incremental Spotify listening-history synchronization

Status: revised proposal for issue #73 after one adversarial design round. Implementation is not
included.

## Cadence amendment (2026-10-02)

The approved implementation originally paired a daily history schedule with a 20-hour freshness
window. The later user-approved operating contract supersedes that cadence: history-only schedules
run every 360 minutes and terminal success stays fresh for 345 minutes. The 15-minute margin is
larger than the ten-minute refresh deadline, so a check that finishes ten minutes after launch is
eligible at the next scheduled launch. Other schedule kinds retain their 1,440-minute cadence.
The request ceiling, no-retry behavior, lock, deadline, quota/cooldown handling, schema version,
unknown-duration reporting, and incomplete-gap guarantees remain unchanged. Current-behavior
sections below reflect this amendment; historical discussion of the earlier decision is labeled
as superseded where it appears.

## 1. Problem

The one-time Spotify extended-history archive import populates `listening_history`, but later
refreshes do not append recently played tracks. The local history therefore becomes stale at the
archive cutoff even when a general refresh succeeds.

The existing table also assumes archive-quality evidence: every row has an exact
`milliseconds_played`, archive digest, and import time. Spotify's recently-played response instead
provides a play timestamp, track metadata, and optional context. It does not provide actual
listening time, completion, or skip state. Track `duration_ms` describes the recording and must not
be treated as observed listening time.

**Goal:** Design incremental Spotify listening-history synchronization after a one-time archive
import, so recent listening questions use current local evidence while making only verified,
necessary requests within the existing rate-limit controls.

### Verified provider contract

The design depends only on the following documented behavior:

| Contract item | Verified behavior | Source |
| --- | --- | --- |
| Endpoint | `GET /v1/me/player/recently-played` returns the current user's recently played tracks; podcast episodes are unsupported. | [Get Recently Played Tracks](https://developer.spotify.com/documentation/web-api/reference/get-recently-played) |
| Permission | OAuth scope `user-read-recently-played` is required. | [Endpoint reference](https://developer.spotify.com/documentation/web-api/reference/get-recently-played), [Scopes](https://developer.spotify.com/documentation/web-api/concepts/scopes) |
| Request fields | `limit` is 1 through 50. `after` and `before` are mutually exclusive Unix timestamps in milliseconds and are exclusive bounds. | [Endpoint reference](https://developer.spotify.com/documentation/web-api/reference/get-recently-played) |
| Response envelope | The response documents `items`, `next`, `cursors.after`, `cursors.before`, and `total`. Each item contains `track`, `played_at`, and `context`. | [Endpoint reference](https://developer.spotify.com/documentation/web-api/reference/get-recently-played) |
| Track fields used | The documented track object includes `uri`, `name`, artists, album, `duration_ms`, and `is_local`. | [Endpoint reference](https://developer.spotify.com/documentation/web-api/reference/get-recently-played) |
| Rate limits | Calls count in a rolling 30-second app window. A 429 response is the rate-limit signal; the existing transport already maps `Retry-After` into durable pacing state. | [Rate Limits](https://developer.spotify.com/documentation/web-api/concepts/rate-limits) |

### Explicit unknowns

Spotify's public endpoint documentation does not state a time-based retention period, a guaranteed
maximum history size, ordering guarantees, whether identical `(played_at, track)` observations can
represent multiple real plays, or whether a cursor remains usable across later calls. `total` is
documented but its stability and relationship to retained history are not. The design therefore
does not claim that `after=<old checkpoint>` can recover every play since an archive cutoff.

The archive importer's `ts` and the API's `played_at` are not established to denote the same
instant or use the same precision. The importer reads `ts` and includes the archive field tuple
and occurrence index in its event identity
([importer](../../src/music_friend/store/spotify_history.py)). Timestamp equality across sources
is therefore only a candidate correspondence, never proof of one play. Unequal timestamps do not
prove that observations describe different plays either.

The endpoint documents no quota-free dry-run. A request-plan preview can be computed locally, but
it cannot confirm permission, provider retention, pagination behavior, or response shape. Any such
confirmation consumes a real request and remains deferred to a separately approved bounded live
validation.

## 2. Approach

Add listening history as an independently reported component of the existing local refresh. It
uses the existing Spotify adapter, transport, adaptive `_PacedSource`, persisted `source_limits`,
refresh lock, deadline, CLI connection flow, and daily schedule. A history check never triggers a
health probe or catalog refresh.

The sync is an overlap-first incremental poll:

1. If Spotify is cooling down, the history permission is absent, or the component is still fresh,
   make no request and report the specific state.
2. Otherwise request one 50-item page with `after` derived from the durable polling timestamp
   boundary, or omit `after` on the first check. This boundary provides a small request overlap;
   it is not a completeness checkpoint.
3. Follow only a validated provider continuation within this invocation, subject to the fixed
   request, page, and refresh-deadline budgets below. Never persist or restore a provider cursor.
4. Normalize each accepted page, then commit API observations, the greatest committed timestamp,
   and any incomplete-interval record in one transaction. A terminal response updates the
   successful-check time separately; a partial run can advance the polling boundary without
   claiming success or complete coverage.
5. Deduplicate exact API observations idempotently. Preserve archive events independently and
   report source-specific counts, possible duplicate observations, and unknown or incomplete
   intervals rather than inventing a cross-source play identity.

Amend the original daily design to use a 5-hour-45-minute freshness window within a six-hour
history-only schedule. The cadence and freshness are defined by `HISTORY_REFRESH_MINUTES = 360`
and `HISTORY_FRESHNESS_MINUTES = 345` in
[domain models](../../src/music_friend/domain/models.py) and passed by the
[CLI schedule installer](../../src/music_friend/runtimes/cli.py); the freshness convention is
documented in [operations](../operations.md). A terminal successful check makes the component
fresh only while its timestamp is within that window and there is no unresolved later attempt.
Failed, partial, or interrupted checks leave a durable repair-needed flag, so a repair run remains
eligible subject to the same cooldown, request budget, and deadline. The schedule normally supplies
one check every six hours; freshness also suppresses redundant manual checks. It is not a separate
quota guarantee.

The history component may run during `refresh catalog` and `refresh all` because both already
open Spotify. No new daemon, hosted service, standalone scheduler, MCP write tool, or implicit
reconnection is introduced.

### Request budget and stopping conditions

| Trigger/state | Request | Maximum | Boundary behavior | Cooldown and stopping behavior |
| --- | --- | --- | --- | --- |
| Scheduled/manual refresh; last terminal successful history check is under 5 hours 45 minutes old and no later attempt needs repair | None | 0 | Preserve polling boundary. | Stop locally as `skipped_fresh`. |
| Permission absent | None | 0 | Preserve checkpoint. | Stop locally as `permission_required`; reconnect is an explicit CLI action. |
| Persisted Spotify cooldown or quota exhaustion | None | 0 | Preserve checkpoint. | Stop locally as `cooling_down` or `quota_exhausted`; do not probe. |
| First eligible check | `GET /v1/me/player/recently-played?limit=50` | 1 page normally; 2 endpoint attempts and 100 accepted items hard maximum | Commit accepted observations and their greatest timestamp. A continuation is invocation-local only; initial retention remains unknown. | Stop on empty page, missing next cursor, repeated/non-advancing cursor, attempt/item budget, 429, invalid response, or shared refresh deadline. |
| Later eligible check | Same endpoint with `limit=50&after=<overlap boundary>`; a second request may use a validated invocation-local continuation | Same maximum | Convert the greatest committed API timestamp to Unix milliseconds, round down, subtract one millisecond, and clamp to zero. This overlaps the boundary observation under the documented exclusive bound, without asserting lossless recovery. Partial accepted pages may advance the boundary; the abandoned interval stays incomplete. | Same stops. A 429 uses existing `Retry-After`, pause budget, AIMD window, and durable `source_limits`; no history-specific retry loop. |
| Local status or summary | None | 0 | Read stored evidence and coverage only. | Never opens a provider, refreshes a token, or checks health. |

The two-attempt ceiling counts actual recently-played endpoint requests, including any retry;
it is not merely a count of logical pages. Two pages are an allowance, not an assertion that
Spotify exposes 100 retained plays. A validated continuation can be used only while the same
invocation has budget left. If the second page advertises more data, stop as `budget_exhausted`,
discard its continuation, and retain a durable incomplete interval. A full page alone does not
prove that more items exist; provider retention and ordering remain unknown even after a terminal
response.

The next eligible run starts from the greatest committed timestamp rather than repeatedly
restarting an unfinished walk at the older successful boundary. This permits local polling
progress in bounded fixtures at the cost of potentially abandoning unseen older observations.
The design explicitly accepts that loss: no ordering, retention, or lossless-resume guarantee is
claimed. Invalid pages contribute no observations or boundary advancement; previously committed
pages retain their incomplete status. The shared refresh deadline remains authoritative even
when fewer than two requests were made.

## 3. Components

### Provider contract and Spotify adapter

- `src/music_friend/providers/base.py`: add a provider-neutral `RECENT_PLAYS` capability and a
  bounded recent-play page contract with an initial timestamp boundary and invocation-local
  continuation. The normalized item carries provider-native track URI, display metadata, UTC
  `played_at` without precision loss, and optional context only.
- `src/music_friend/providers/spotify/scopes.py`: map `RECENT_PLAYS` to
  `user-read-recently-played`. Existing tokens without it remain connected but cannot run history
  sync. Scope changes happen through the current explicit CLI connection flow.
- `src/music_friend/providers/spotify/transport.py`: add the fixed recently-played operation and
  allowlisted query fields. Reuse body limits, content-type validation, deadlines, 429 mapping,
  and safe errors.
- `src/music_friend/providers/spotify/source.py`: validate the envelope, timestamps, track URI,
  names, and cursor. Ignore response URLs as navigation instructions; reconstruct the next request
  from validated cursor data. Do not infer listen duration or skip state.

### Durable history model

Keep `listening_history` and its importer unchanged. Its required `milliseconds_played` and
`archive_digest` remain archive guarantees
([schema](../../src/music_friend/store/schema/008_listening_history.sql)); its occurrence-sensitive
IDs continue to preserve repeated otherwise identical archive rows.

Add a migration under `src/music_friend/store/schema/` creating `recent_play_observations`, an
API evidence store separate from archive events, with:

- provider and deterministic observation key, unique within that provider;
- UTC `played_at`, preserving supplied precision rather than rounding it for identity;
- provider-native track URI, track name, artist name, album name, and optional context;
- local `observed_at` ingestion time and indexes for time/source queries.

Do not populate actual listening duration, skip, or completion fields for API observations.
Track duration may remain recording metadata but is never an observed listening-time substitute.
Source provenance is explicit in the separate stores; no archive-table rebuild, nullable archive
constraints, or cross-source evidence child table is needed.

API observation keys use normalized `(played_at, track_uri)` without precision loss. Overlapping
API pages insert that observation once; distinct timestamps remain distinct observations.
This is an idempotence key, not a provider-guaranteed event identity. Identical keys may represent
multiple real plays, so API counts are observation counts with possible undercount disclosed.

Archive and API observations remain independently recoverable. No row is deleted or rewritten
because of cross-source timestamp equality, regardless of import order. Read-time comparison may
report exact-key candidate overlaps, including ambiguous multiplicity when several archive events
share a candidate key. It never silently collapses them. All combined counts within overlapping
source intervals remain potentially duplicated observation totals, even if no exact-key candidate
exists: differing timestamp semantics can hide correspondences. Source-specific counts remain
available; there is no assertion of a unique combined play count.

### Checkpoints and coverage

Add a history sync state record keyed by provider with:

- `last_attempt_at`: local time of the latest attempt;
- `last_successful_check_at`: local time of the last terminal, committed check;
- `needs_repair`: set durably before issuing an eligible check, cleared only by terminal success;
- `newest_observed_played_at`: greatest API play timestamp committed, nullable after an empty
  first response; this is also the monotonic polling timestamp boundary;
- `requested_after_ms`: the initial request boundary of the latest attempt, nullable on first check;
- `attempt_outcome`: facts such as `running`, `first_snapshot`, `terminal_empty`,
  `terminal_nonempty`, `bounded_partial`, `failed`, and the local skip states in the budget table;
  `first_snapshot` denotes a terminal first nonempty check, not complete initial history;
- `interval_completeness`: `archive_complete`, `incomplete`, or `unknown`, with durable interval
  bounds and reasons; `archive_complete` requires independently established contiguous archive
  coverage, not merely the first and last imported timestamps;
- `coverage_reason`: closed values including `first_check_retention_unknown`, `budget_exhausted`,
  `rate_limited`, `invalid_response`, `permission_required`, and `cooling_down`.

Query `archive_first_played_at` and `archive_last_played_at` directly from archive evidence when
producing local summaries/status. API ingestion does not advance those bounds, and a later archive
import is visible without changing the archive importer or maintaining a second cutoff cache.

Provider cursors exist only in invocation memory. The durable timestamp boundary represents
observations, while `last_successful_check_at` controls freshness. Neither establishes complete
coverage. Empty terminal success advances check freshness but not the polling boundary. A partial
or failed attempt does not advance successful-check time, even when earlier accepted pages have
advanced the polling boundary. `needs_repair` makes a later failed/partial attempt invalidate
freshness even if a previous successful check is still under 5 hours 45 minutes old. A process exit while
`running` leaves that flag set; local skip states do not clear it.

For each page, atomically insert idempotent observations, advance the boundary to the greater of
its prior value and the accepted timestamps, and record an incomplete interval from the attempt's
starting boundary through the new boundary. On the first check there is no known starting boundary;
record a retention-unknown snapshot instead of inventing historical gap bounds. Terminal success
updates attempt facts and freshness and clears `needs_repair` in the final transaction. It does
not erase pre-existing gaps or promote API intervals to complete. If a run stops before termination, the incomplete interval
and stop reason survive the discarded cursor. If it commits no observations, retain the prior
boundary and report the failed/partial attempt without fabricated gap bounds.

A crash before commit leaves the previous durable boundary; the next run replays from its overlap.
A crash after commit starts from the advanced timestamp boundary, with the incomplete interval
already durable. It never restores a provider cursor or claims to recover unseen older pages.
This sacrifices lossless catch-up to avoid repeatedly consuming the same bounded window. A later
archive can provide independent evidence for an interval, but API-only and mixed intervals remain
unknown or incomplete unless stronger coverage evidence is established.

### Summaries and status

`summarize_listening_history` remains local and gains evidence-aware totals:

- preserve the archive meaning of existing `play_count`; add separate `api_observation_count`
  and `combined_observation_count` rather than redefining it as unique mixed-source plays;
- report candidate-overlap counts and ambiguity explicitly; mixed-source totals within overlapping
  intervals are potentially duplicated observations, including when candidate count is zero;
- `milliseconds_played` sums archive values only;
- `duration_observation_count` and `duration_unknown_count` identify archive and API denominators;
- skip, brief-play, completion, and listening-time rankings use archive rows with the required
  observed fields only; track duration never fills missing listening time;
- recent-artist rankings can use source-specific observation counts; any combined ranking is
  labeled as based on potentially duplicated observations, not unique plays or listening time;
- history evidence does not create or change an explicit preference.

CLI diagnostics and MCP status expose the same allowlisted history block: last attempt outcome,
last successful check, newest observed play, archive cutoff, interval completeness/reason, and retry
time.
A general refresh can succeed while the history attempt is `permission_required`, `bounded_partial`,
or `cooling_down`, or its interval completeness remains `unknown`; its output must carry those
independent facts rather than imply completeness. Neither provider-native track IDs nor raw cursor
values appear at the MCP boundary.

Synthetic example: archive evidence ends at `2030-01-01T10:00:00Z`; API observations begin at
`2030-01-01T10:05:00Z`; the first API response cannot prove what happened in the five-minute
interval. A query spanning that interval returns source-specific observation counts plus
`interval_completeness: incomplete` and the gap bounds. If both sources contain `Track A` at
`2030-01-01T10:05:00.123Z`, retain both rows and report a candidate overlap. Its archive milliseconds
remain the only known listening-time value. If their timestamps differ, the absence of an exact
candidate still does not make a combined total a unique play count.

### Portability, deletion, and documentation

- `src/music_friend/store/portable.py`: bump the portable format to export/import the separate API
  observation store, polling boundary, attempt/freshness facts, and incomplete intervals. Continue
  accepting older exports as archive-only evidence with no invented API state. Restore preserves
  unknowns and gaps; it neither imports provider cursors nor reconciles away either source.
- Existing source purge and full local-data deletion remove both history observations and history
  sync state. Spotify disconnect removes credentials but retains local evidence, matching current
  catalog behavior; document the separate deletion command.
- Update `docs/operations.md`, `docs/mcp.md`, `docs/limits.md`, `docs/setup.md`, and
  `docs/troubleshooting.md` with consent, status semantics, unknown-duration behavior, request
  budget, backup/restore/delete coverage, and safe diagnostics. Use synthetic values only.
  During implementation, keep the six-hour history refresh description aligned with its
  5-hour-45-minute freshness window and the CLI installer.

## 4. Data Flow

```text
six-hour/manual refresh
        |
        v
local freshness + permission + source_limits checks
        | eligible
        v
durably record attempt + set needs_repair
        |
        v
existing _PacedSource -> Spotify adapter -> recently-played endpoint
        |                         |
        | 429/deadline/invalid    | validated pages (max 2 / 100 items)
        v                         v
durable cooldown/partial     normalize observations
                                  |
                                  v
                       page-level database transaction
                       - insert idempotent API observations
                       - advance polling timestamp boundary
                       - persist incomplete interval/reason
                       - on terminal response, update check time
                                  |
                                  v
                     local source-specific summaries/status

provider continuation: invocation-local only; discarded at exit
archive events: unchanged; candidate overlaps are read-time evidence only
```

## 5. Acceptance Criteria

These criteria apply to the future implementation. Create
`tests/providers/spotify/test_recent_plays.py`, `tests/tools/test_history_sync.py`, and
`tests/store/test_recent_play_observations.py`; the other named test files already exist.

- [ ] The provider layer exposes a provider-neutral recent-play capability; Spotify maps it to
  `user-read-recently-played`, uses only the documented endpoint/query fields, validates synthetic
  page/cursor fixtures, and never follows response URLs directly — verified by:
  `tests/providers/spotify/test_recent_plays.py` and `tests/providers/spotify/test_scopes.py`.
- [ ] History sync makes zero provider calls when fresh, missing permission, cooling down, or quota
  exhausted; freshness uses a terminal successful check, no repair-needed attempt, and the existing
  5-hour-45-minute window. Otherwise it makes at most two actual 50-item endpoint attempts, shares the
  existing refresh deadline and pacing state, honors 429 cooldown across runs, and stops on empty, repeated cursor,
  non-advancing cursor, budget, or deadline — verified by:
  `tests/tools/test_history_sync.py` with a request-counting fake source.
- [ ] A separate API observation store represents actual duration/skip/completion as unknown,
  deduplicates overlapping API pages without timestamp precision loss, and preserves distinct
  timestamps. The archive table's constraints, occurrence identities, and import behavior stay
  intact. Neither import order nor a cross-source candidate match deletes or rewrites either
  source; candidate reporting is deterministic and does not assert timestamp equivalence — verified
  by: proposed `tests/store/test_recent_play_observations.py`, existing
  `tests/store/test_spotify_history.py`, and migration tests using synthetic rows.
- [ ] Durable state distinguishes last attempt, last successful check, newest observed play,
  archive cutoff, polling timestamp boundary, cooldown, and interval completeness. Observations,
  monotonic boundary advancement, and incomplete-interval records commit atomically. Repair-needed
  state survives a later failure or process interruption and clears only on terminal success.
  Empty terminal success advances check time only; partial/failure never advances successful-check
  time. Provider cursors are invocation-local. Restart is idempotent, can progress beyond a repeatedly full bounded
  fixture window, and preserves the uncertainty about abandoned items — verified by:
  `tests/tools/test_history_sync.py` and proposed `tests/store/test_recent_play_observations.py`.
- [ ] Local summary and status outputs report incomplete intervals and known-duration denominators,
  source-specific counts, and potentially duplicated combined observations even without exact-key
  candidate matches. They never infer unique mixed-source play counts, duration, skip/completion,
  or complete API coverage, never call Spotify, and do not turn listening evidence into explicit
  preference — verified by: `tests/store/test_spotify_history.py`, proposed
  `tests/store/test_recent_play_observations.py`,
  `tests/mcp/test_catalog_server.py`, and request-denying local-query tests.
- [ ] Portable export/import, source purge, full deletion, and disconnect behavior cover the new
  observations and checkpoints while remaining backward compatible with the prior portable format
  — verified by: `tests/store/test_export.py`, `tests/store/test_import.py`,
  `tests/store/test_purge.py`, and Spotify disconnect tests.
- [ ] Public fixtures and documentation contain only synthetic project data and document consent,
  the request budget, unknown retention, unavailable listen-duration/skip fields, coverage states,
  and backup/restore/deletion behavior — verified by: documentation review and
  `scripts/scan_public_tree.py`.

### Implementation roadmap

Implement as one ordered feature branch because schema semantics constrain every later surface:

1. Add domain capability, normalized recent-play types, Spotify scope/transport/source support,
   and synthetic provider tests.
2. Add the separate observation-store migration, idempotent API inserts, read-time candidate
   comparison, atomic timestamp-boundary/incomplete-interval transaction, portable-format
   compatibility, and store tests. Preserve archive constraints and importer behavior.
3. Add the bounded history refresh component using existing pacing/freshness/deadline state, then
   wire it into current refresh and schedule surfaces.
4. Add source-specific local summaries, ambiguity labels, and allowlisted CLI/MCP status fields.
5. Update public operations, setup, MCP, limits, troubleshooting, and changelog documentation.

Do not split the work into independently shippable tracks: adapter output, schema identity, and
checkpoint advancement must agree before any live-account validation is meaningful.

### Validation plan

All development and required merge validation use offline synthetic fixtures. No Spotify request is
needed to accept the implementation.

| Scenario | Expected proof |
| --- | --- |
| Repeated/overlapping pages | Same API observations insert once; advancing items insert once; request count stays within two. |
| Archive/API overlap in either import order | Both sources remain independently recoverable; exact-key candidate counts are deterministic and non-destructive; differing timestamps do not prove distinct plays. |
| Repeated same track | Distinct timestamps remain distinct; exact same API key is idempotent; identical archive occurrences remain representable. |
| Timestamp boundary | Identity preserves supplied precision; millisecond floor minus one includes the committed boundary observation under a fixture with the documented exclusive bound. This does not prove provider retention or ordering. |
| Missing permission | Zero requests, checkpoint unchanged, explicit reconnect-required status. |
| Freshness and cadence | A six-hour history schedule uses the 5-hour-45-minute terminal-success freshness window; a check that succeeds ten minutes after launch is eligible at the next launch. A fresh manual check makes zero calls. A later partial/failure/interruption invalidates freshness even if the older successful timestamp is still recent; repair remains subject to shared pacing. |
| Empty response | Successful-check time advances, polling timestamp does not; `terminal_empty` does not distinguish no new plays from expired provider history. |
| 429 across runs | Existing `Retry-After` state persists; the next run before retry time makes zero requests. |
| Partial second page, invalid page, or deadline | Accepted observations, monotonic polling boundary, and incomplete interval commit together; successful-check time stays unchanged. Invalid pages contribute nothing; no cursor is persisted. |
| Repeatedly full bounded window | Under a specified fake ordering, the next invocation starts from its greatest committed timestamp instead of repeating the old window; abandoned observations remain an explicit incomplete interval. No general lossless-progress guarantee is inferred. |
| Crash around commit | Before commit, restart uses the prior boundary and inserts API observations once; after commit, restart uses the advanced boundary and retains its incomplete interval. Neither restores a cursor or promises unseen-page recovery. |
| Retention gap | Summary returns available evidence with explicit incomplete bounds; no backfill claim is made. |
| Cross-source summary ambiguity | Preserve archive `play_count`, expose API/source-specific observation counts, and label combined counts/rankings potentially duplicated even when no exact candidate matches. |
| Local query/status | A network-denying fake proves zero provider, token-refresh, health, or catalog calls. |
| Portability/deletion | Round trip preserves unknowns and coverage; purge/delete removes both observations and sync state. |

Any later live validation requires new exact authorization and a written plan created before the
call: one `limit=1` recently-played request, no health or catalog probe, no automatic retry, stop on
any 401/403/429/invalid response, record only redacted counts and field-presence facts, and delete
raw response material after comparison. This single call can confirm permission and top-level
shape only; it cannot establish archive/API timestamp semantics or precision equivalence, event
identity, ordering, retention, completeness, pagination, or deduplication correctness. The design
does not depend on those guarantees, so this call is not an identity-validation gate.

## 6. Out of Scope / Future PRs / Deferred

### Out of scope for this PR

- Production implementation, live-account requests, credential inspection, archive/database
  inspection, and schedule installation.
- Historical gap repair beyond a later archive import. The recently-played endpoint is not treated
  as a complete backfill service.
- Podcast listening, playback control, hosted polling, a resident daemon, or an MCP connection/
  consent mutation surface.
- Deriving preference, affinity, skip, completion, or listening time from track duration or play
  presence.

### Future PRs

The implementation may follow issue #73 after this design is approved. No additional issue
decomposition is required by this design.

### Deferred decisions

- Whether Spotify returns more than one page reliably and how long observations remain available;
  keep these unknown until bounded, explicitly approved evidence exists.
- Whether same-track plays with identical provider timestamp precision can be distinguished. The
  documented response supplies no event identifier, so coverage reports the possible undercount.
- Whether archive `ts` and API `played_at` ever support a stronger correspondence rule. Separate
  evidence and observation labels remain authoritative until that is established; neither a
  synthetic exact match nor one live request supplies that evidence.

## 7. Risks

1. **Irrecoverable gaps:** a missed polling interval may exceed undocumented provider retention.
   Mitigation: bounded six-hour polling and explicit incomplete intervals. Budget exhaustion and
   restart may also abandon older observations; a later archive is the only proposed historical
   repair source, and merely importing its first/last timestamps does not prove complete coverage.
2. **False deduplication at provider timestamp precision:** identical API keys may hide distinct
   plays. Mitigation: retain distinct timestamps, preserve archive occurrence identities, and name
   the API-only ambiguity in coverage rather than inventing identity.
3. **Rate-limit contention:** history calls share Spotify's app quota with catalog work.
   Mitigation: 5-hour-45-minute terminal-success freshness gating, two-attempt hard ceiling, existing
   adaptive pacing/cooldowns, and no probes or token/catalog calls from local reads.
4. **Cross-source ambiguity:** archive and API timestamps may describe different instants or
   precision, hiding or creating apparent overlaps. Mitigation: separate stores, deterministic
   candidate reporting, source-specific counts, and potentially duplicated combined observation
   labels. This adds read complexity and does not yield a unique mixed-source play total.

## Handoff

- **Issue:** #73
- **Files to create:** provider/store/tool tests and one schema migration named by the implementer
  according to the next migration number; a dedicated recent-play observation store module
- **Files to modify:** provider capability and Spotify adapter files; history store and portable
  format; refresh, CLI/MCP status, and the documentation listed above
- **Tests to add:** the synthetic scenarios in the validation plan
- **Sequence:** provider contract, storage/checkpoint transaction, refresh integration, local
  summaries/status, portability and docs, offline full-suite validation

## Review trail

One independent adversarial round reviewed the original proposal. Its verdict was
`MAJOR_CONCERNS`; the revisions below are the author's response, not a second reviewer approval.

- **Author A's original shape:** evolve the archive table, merge archive/API events by timestamp
  and track URI, and persist provider continuations across refreshes.
- **Reviewer B's counter-shape:** keep a separate API observation store, compare sources at read
  time, and restart partial walks from the previous successful boundary. **Trade:** preserve
  archive guarantees and revisable evidence while accepting query complexity and repeated fetches.
- **Selected shape:** separate stores with candidate-only comparison; invocation-local provider
  cursors; advance the durable polling timestamp with accepted observations and incomplete-interval
  records. This avoids repeatedly fetching the same bounded fixture window at the explicit cost
  of potentially abandoning unseen older observations. It does not promise lossless recovery.

| Finding | Goal/invariant mapping | Author disposition and revision |
| --- | --- | --- |
| Archive-table rewrite and destructive cross-source identity | Evidence honesty and reversibility | Concede; preserve archive constraints and importer, add a separate API store, and retain both sources in either import order. |
| Unverified `ts`/`played_at` equivalence | Accurate listening answers | Concede; equality is candidate evidence only. Combined observations can duplicate real plays even when exact candidate count is zero. |
| Cross-run cursor stability and restart starvation | Useful bounded ongoing sync | Concede cursor risk; use an invocation-local cursor. Amend the restart counter-shape with atomic polling-boundary progress and durable incomplete intervals. |
| Coverage wording | Honest local summaries | Concede; separate attempt facts from interval completeness. Neither empty success nor API timestamp advancement proves complete coverage. |
| Daily cadence and 12-hour freshness | Verified scheduling and request minimization | Superseded by the approved six-hour history cadence and 5-hour-45-minute freshness constants; other schedule kinds remain daily. |
| Single-request live validation | Source discipline | Concede; permission/envelope confirmation only. Remove timestamp-equivalence and lossless-resume dependencies rather than treating one request as proof. |
| Test-file existence and fit | Executable implementation AC | Existing cited files are retained where relevant; use a dedicated proposed `tests/store/test_recent_play_observations.py` for new store and checkpoint assertions. |

All in-scope findings have a concrete disposition in this proposal. Implementation and live
validation remain outside this design change.
