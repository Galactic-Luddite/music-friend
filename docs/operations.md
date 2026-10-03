# CLI and data operations

Run `music-friend --help` for the complete command grammar. Commands that inspect or refresh local
state support `--json` as their final argument. Credentials and whole-catalog lifecycle operations
(imports, exports, backups, restores, and deletion) are CLI-only. Both the CLI and MCP can run
bounded refreshes; MCP also owns watchlist and inbox decisions and exposes status, catalog search,
and listening-history summaries.

| Group | Commands | What they do |
|-------|----------|--------------|
| Setup and connection | `doctor`, `setup`, `connect spotify`, `disconnect spotify`, `version` | Check every onboarding prerequisite locally, enter the Spotify client identifier and optional event area, authorize or remove the Spotify credential |
| Inspect | `status`, `diagnostics`, `watchlist list`, `inbox list`, `inbox show ITEM_ID` | Read local state without contacting a provider |
| Refresh | `refresh catalog`, `refresh history`, `refresh releases`, `refresh events`, `refresh all` | Read a provider and write results to the local catalog |
| Data lifecycle | `data export`, `data backup`, `data import`, `data import-spotify`, `data restore`, `data delete`, `data inbox duplicates`, `data inbox unmerge` | Move, protect, or erase local data |
| Schedule | `schedule status`, `schedule install`, `schedule remove` | Manage the optional daily refresh |
| Skill | `skill install` | Install the optional runtime skill for an AI client |

## Agent-driven setup

All setup operations support non-interactive flags and JSON output for automation:

```bash
# Non-interactive setup (TTY optional)
music-friend setup --spotify-client-id <id> --event-country US --event-postal 94110 --event-radius 50 --event-unit miles --release-sources musicbrainz

# Clear individual fields
music-friend setup --clear-spotify-client-id

# Ticketmaster key from environment variable
music-friend setup --ticketmaster-key-env TICKETMASTER_KEY

# Ticketmaster key from file (must be chmod 600)
music-friend setup --ticketmaster-key-file /path/to/key

# Ticketmaster key from stdin
cat /path/to/key | music-friend setup --ticketmaster-key-stdin

# Structured output
music-friend setup --spotify-client-id <id> --json
```

**Secret handling:** The Ticketmaster key never appears in stdout, stderr, JSON output, exception messages, or logs. It is read from the specified source (env, file, or stdin) and stored securely in the operating system credential store.

**MCP setup tools:** Agents can query and update setup programmatically via MCP:
- `get_setup`: Reports configured fields (without secret values), which fields are missing for MCP readiness, and what gaps remain
- `update_setup`: Updates non-secret configuration fields via MCP; secrets must be set through CLI

**Backward compatibility:** Running `music-friend setup` with no flags continues to prompt interactively, preserving existing workflows.

## Destructive data commands with confirmation

Data operations that erase local state now support non-interactive confirmation:

```bash
# Automatic confirmation (no prompt)
music-friend data delete --yes
music-friend data restore backup.json --yes

# Token-based confirmation (no TTY required)
music-friend data delete --confirm DELETE
music-friend data restore backup.json --confirm RESTORE
```

Without a TTY and without `--yes` or `--confirm`, these commands refuse to proceed and exit with code 2.

## Cross-subject duplicate releases

```bash
music-friend data inbox duplicates
music-friend data inbox duplicates --merge --yes
music-friend data inbox unmerge MERGE_ID --yes
```

Two releases discovered through different sources or before a title-normalization fix can end up
in distinct inbox subjects for the same real-world release. `data inbox duplicates` is dry-run by
default: it scans stored releases for pairs in different subjects that share a folded title, the
same artist set, and the same release date, and lists each pair with its titles and both subjects'
inbox states. It also lists every open identity conflict (a source linked two releases that already
had separate inbox items; Music Friend never merges those on its own) under `conflicts`, and as a
pair with tier `external_link`. It contacts no provider and writes nothing.

`--merge --yes` merges every listed pair: the later release's subject is re-pointed onto the
earlier one's, the two inbox entries collapse (the more-decided one wins; ties go to the more
recently updated, then to the entry with the smaller id), and a merge id is printed for each
merged pair. Merging a pair closes its open identity conflicts, which `music_status` then stops
counting. Run `data inbox duplicates` again afterward to confirm it lists nothing -- every merged
pair now shares one subject.

`data inbox unmerge MERGE_ID --yes` reverses one merge: it restores the pre-merge inbox state,
signal, and timestamps and re-points the affected releases back to their original subjects, but
only if nothing has changed the winning entry since the merge (compare-and-swap on its
`updated_at`). If the user made a new decision on the winner after the merge, the unmerge is
refused and the reason is printed; nothing is changed.

This command replaces the retired `music-friend data dedupe-inbox` (see CHANGELOG).

## Check prerequisites

```bash
music-friend doctor
music-friend doctor --json
```

`doctor` reports Python, the native credential store, the Spotify client ID and connection, the
event area, and the Ticketmaster key in one pass. Each check is `ok`, `failed`, or `unchecked`
(credential checks are skipped when no native store is available, so the vault is never
unlocked), and every non-`ok` check names its fix. It exits 0 when everything is ready and 5
otherwise. It reads only local state, never contacts a provider, and never prints credential
values, so its output is safe to paste into an issue.

It also reports every configured release source (not only the first one). When MusicBrainz is
among them, the report includes the number of unmapped watchlist artists and directs you to
`music-friend refresh releases` to map them.

`status --json` includes `mcp_ready`, which is `false` when the MCP server could not open an
approved native credential store.

## Inspect and refresh

```bash
music-friend status --json
music-friend refresh catalog --json
music-friend refresh catalog --force --json
music-friend refresh history --json
music-friend refresh releases --json
music-friend refresh events --json
music-friend refresh all --json
music-friend watchlist list --json
music-friend inbox list --json
music-friend inbox show ITEM_ID --json
music-friend diagnostics --json
```

A refresh reads Spotify (and Ticketmaster for `events`) and writes only to your local catalog. It
never changes a provider account. A refresh can end as `partial` when a provider limit interrupts
it; a later refresh resumes where it stopped. See [provider limits](limits.md).

### Adaptive request pacing

Spotify's request rate is learned per 30-second window, stored locally and carried from run to
run. A 429 halves it; a streak of successful requests slowly grows it back, up to the built-in ceiling.
This means a large library converges on the fastest rate Spotify will accept instead of retrying
at a fixed pace every run. When a fallback delay is estimated (no exact `Retry-After` from the
provider), it is jittered within its ladder step so multiple installs recovering at the same time
do not retry in lockstep. MusicBrainz is not learned: it always runs at its documented one request
per second, and a stored rate from an earlier version never slows it down.

Release discovery also skips an artist entirely -- no source request -- when its releases were
successfully checked within the last 20 hours, so a second full refresh soon after the first makes
requests only for artists that are actually due for a check. This freshness window is kept below
the scheduled daily refresh's 24-hour cadence so a scheduled run that starts slightly early never
mistakes every artist for fresh and silently skips a whole cycle. The Spotify adapter has no batch
lookup endpoint today (it calls `/v1/artists/{id}/albums` per artist), so batching several artists
into one call and conditional (`If-None-Match`) requests remain future work.

`refresh catalog` applies the same freshness skip per capability (followed artists, saved items,
and each top-items time range): a capability that completed successfully within the same freshness
window makes zero requests on the next run, so a daily refresh spends its request budget on
release discovery instead of re-paginating an unchanged library. A capability that ends `failed`
never marks itself fresh, so the next run retries it in full. Pass `--force` to bypass the skip and
re-paginate every catalog capability regardless of freshness -- for example, after connecting a
different Spotify account or when you want to force a full re-sync:

```bash
music-friend refresh catalog --force --json
```

The refresh result's metrics include a `catalog_skipped_fresh` count of how many catalog
capabilities were skipped as fresh, and `status --json` surfaces the same count from the latest
run, so a script can tell "made few requests because everything was fresh" apart from "made few
requests because it failed early".

A `partial` refresh result includes three extra fields when the cause is known:

- `reason`: `rate_limited`, `quota_exhausted`, `deadline` (the ten-minute refresh deadline was
  reached), or `source_errors` (some artists or capabilities failed while others succeeded). Every
  `partial` result carries a `reason`.
- `retry_after`: an ISO-8601 timestamp for when the source is expected to be ready again (only for
  `rate_limited`; omitted for `quota_exhausted`, which has no known reset time, and `deadline`).
- `remaining`: how many records this run skipped rather than completing.

The text (non-`--json`) output appends them to the summary line, for example:

```text
Refresh releases: partial. reason=rate_limited retry_after=2026-09-24T18:05:00+00:00 remaining=6
```

A refresh started while another one holds the local refresh lock makes no provider request and
reports `{"status": "partial", "reason": "already_running", "retry_after": "..."}`, where
`retry_after` is when the held lock becomes stale and can be taken over (omitted if the lock file
is unreadable).

The `source_requests` metric counts provider operation calls made by the run -- Spotify,
MusicBrainz (identity mapping included), Deezer, and Ticketmaster. Local preflight checks and
separate OAuth token activity are outside that count. `signals_repaired` counts inbox signals
reconstructed at the start of a run for discoveries an earlier, interrupted run committed without
their signal; they are reported separately so `signals_created` counts only the current run's own
findings.

`status --json` and the MCP `music_status` tool both report a `source_limits.spotify` block so a
caller can tell whether the source is ready now or when it will be, without starting a refresh.

A `refresh events` run with no event area configured makes no Ticketmaster request and reports
`{"status": "skipped", "reason": "event_area_not_configured"}` instead of `succeeded`, so a script
reading `--json` output can tell "not checked" apart from "checked, nothing found". A `refresh all`
run with the same missing configuration still refreshes catalog and releases normally; its result
adds `events_skipped_reason: "event_area_not_configured"` alongside the run's own `status` so the
events portion is identifiable without the whole run being reported as skipped or failed.

### Refresh command exit codes

`refresh catalog|history|releases|events|all` exits `0` for a `succeeded` or `skipped` outcome, `3` for
`partial`, and `1` for `failed` or an already-running refresh (reported as `partial`). `diagnostics`
and the other inspection commands exit `0` on success and `1` on an unexpected internal error;
`doctor` uses its own `0`/`5` convention documented above.

Diagnostics include a provider-neutral `source_limits` section with availability or cooldown
state, observation and retry times, whether the retry time is exact, consecutive limit count, and
the latest refresh request and pause counts. `state` reflects live cooldown status as of the
diagnostics call: once `retry_at` has passed, `state` reports `available` again even though the
underlying `consecutive_limits` history is preserved for the next limit observation to build on.
It does not include response bodies, headers, or credentials.

Use MCP to make watchlist and inbox state changes. The CLI deliberately exposes only list and show
operations for those records.

## Import Spotify listening history

Request your extended streaming history from Spotify's privacy settings and wait for the ZIP.
This is an optional one-time backfill path. It complements ongoing `refresh history` observations;
it is not required to begin syncing recent listening. Import a newly requested archive later to
fill older periods or gaps that Spotify's recent-play API no longer returns.
Validate it first, then import:

```bash
music-friend data import-spotify my_spotify_data.zip --dry-run --json
music-friend data import-spotify my_spotify_data.zip --json
```

The importer accepts the ZIP from Spotify's extended streaming-history export. It imports
music-track plays only, retains brief and skipped plays as labeled evidence, ignores video,
podcast, and audiobook records, and never stores IP address, device, or connection-country fields.
`--dry-run` validates the complete archive and reports aggregate counts (`imported`, `duplicates`,
`non_music`, `members`, first and last play) without writing plays. Reimporting the same archive
is safe and reports existing records as duplicates.

Imported plays are evidence, not preferences. They do not change the watchlist. Ask questions
about them through the `summarize_listening_history` MCP tool; see the
[MCP guide](mcp.md#listening-history-evidence).

## Export, backup, restore, and deletion

Choose a destination you control, then run one of these local commands:

```bash
music-friend data export music-friend-export.json
music-friend data backup music-friend-backup.json
music-friend data import music-friend-export.json
music-friend data restore music-friend-backup.json
music-friend data delete
```

Restore asks for `RESTORE`; deletion asks for `DELETE`. Exports and backups are portable catalog
data, not a safe place for credentials. Protect them as personal data. Deletion removes local
catalog data only: provider credentials remain. After deletion, run `music-friend disconnect spotify`
to remove the Spotify credential, then rerun setup with `-` at the Ticketmaster key prompt to remove
the Ticketmaster credential. In other words, disconnect Spotify separately before discarding the
local installation.

### Upgrading

Run `music-friend data backup music-friend-backup.json` before upgrading to a new release. A
catalog upgrade applies any pending schema migration automatically the next time it opens (`doctor`
and every other command trigger it); migration 014 collapses a release or event that ever ended up
with more than one inbox item -- for example the same release signalled by two sources, or a repair
pass that re-recorded an already-signalled release under a new explanation -- into the single inbox
item the schema now requires per release or event. The collapse keeps the most-decided entry
(`saved` or `dismissed` beats `unread`; between two decided entries, the more recently updated one
wins) with its original decision, and every entry the collapse touched is preserved, before and
after, in a local snapshot table for later review. No signal is ever deleted or rewritten, and the
collapse never happens without every pre-upgrade duplicate already recorded in that snapshot.
`data restore` of a pre-upgrade backup applies the same collapse on the way in, so restoring an old
backup never fails because of this constraint.

### Data command failure messages

`data export|backup|import|import-spotify|restore` distinguish these failure categories with a
specific, actionable message on stderr (exit `1`) instead of one generic sentence:

- the given file does not exist ("Music Friend could not find the file at ...");
- an export/backup destination already exists ("Music Friend will not overwrite the existing file
  at ...");
- an import or Spotify archive is not a valid export ("Music Friend could not read the archive: it
  is not a valid export.").

`data restore` and `data delete` still exit `2` when the exact confirmation word is not given
("Confirmation was not accepted."). When stdin is not an interactive terminal (so the confirmation
prompt cannot be read at all), they report a distinct message instead of the generic failure
("Confirmation was not accepted: no terminal is attached to read it.") and still exit `2`; nothing
is written or deleted.

No data-command message includes the raw exception text, a resolved absolute path beyond what was
passed on the command line, or any secret or environment value.

### Provider-not-configured

`connect spotify`, `disconnect spotify`, and `refresh catalog|history|releases|all` report a dedicated
message naming the missing provider and pointing at `music-friend doctor` for setup guidance
("Spotify is not configured. Run `music-friend doctor` for setup guidance.") instead of the generic
failure sentence, and exit `1` -- the existing meaning of exit `1` for these commands is unchanged.

## Daily schedule

Package installation and `music-friend setup` never create a schedule. Explicitly installing one
creates a per-user schedule that runs a bounded
`refresh all --json` once every 1,440 minutes with the Python runtime from the Music Friend
installation, then exits. It requires an approved native credential store; the interactive vault
cannot be used for scheduled refreshes.

```bash
music-friend schedule status --json
music-friend schedule install
music-friend schedule install --kind history
music-friend schedule remove
```

Status reports whether the definition is installed and whether the native job is active, together
with its platform and interval. The command uses the current supported platform’s per-user
scheduler: LaunchAgent on macOS or a systemd user timer on Linux. Repeated installation updates the
same named schedule. Remove it when it is no longer wanted.

Pass `--kind history` when the daily job should fetch only recent listening observations. That
job uses the same refresh lock, 20-hour freshness check, deadline, pacing, and durable cooldown
state as other refreshes, while making no catalog, release, event, repair, or provider-health
request. The schedule runs every 1,440 minutes. That cadence reduces gaps but cannot guarantee
none between polls because Spotify's retention window is undocumented and each check is bounded.
Omitting `--kind` keeps the existing `refresh all` schedule.

## Optional skill

`music-friend skill install` copies the packaged runtime skill into a client's skills directory.
See the [install guide](install.md#optional-agent-skill) for destinations and replacement rules.
`refresh history` checks only Spotify recently played history. `refresh catalog` and `refresh all`
also check it before catalog work. The history component remains fresh for 20 hours after a
terminal check and makes no request while fresh, disconnected from the required permission,
cooling down, or quota exhausted. `--force` still honors this 20-hour history freshness window.
An eligible check accepts at most two 50-item pages and reports
its own outcome, operation-attempt/page counts, observations, and coverage facts in JSON. An
operation attempt counts a call to the recently-played operation; local preflight checks and any
separate OAuth token activity are outside that count. Partial checks keep
accepted observations and an incomplete interval without advancing the last successful check.
The recently-played operation itself is never retried after an HTTP or transport failure.
