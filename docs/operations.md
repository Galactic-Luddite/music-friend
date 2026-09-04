# CLI and data operations

Run `music-friend --help` for the complete command grammar. Commands that inspect or refresh local
state support `--json` as their final argument. Credentials and whole-catalog lifecycle operations
(imports, exports, backups, restores, and deletion) are CLI-only. Both the CLI and MCP can run
bounded refreshes; MCP also owns watchlist and inbox decisions and exposes status, catalog search,
and listening-history summaries.

| Group | Commands | What they do |
|-------|----------|--------------|
| Setup and connection | `setup`, `connect spotify`, `disconnect spotify`, `version` | Enter the Spotify client identifier and optional event area, authorize or remove the Spotify credential |
| Inspect | `status`, `diagnostics`, `watchlist list`, `inbox list`, `inbox show ITEM_ID` | Read local state without contacting a provider |
| Refresh | `refresh catalog`, `refresh releases`, `refresh events`, `refresh all` | Read a provider and write results to the local catalog |
| Data lifecycle | `data export`, `data backup`, `data import`, `data import-spotify`, `data restore`, `data delete` | Move, protect, or erase local data |
| Schedule | `schedule status`, `schedule install`, `schedule remove` | Manage the optional six-hour refresh |
| Skill | `skill install` | Install the optional runtime skill for an AI client |

## Inspect and refresh

```bash
music-friend status --json
music-friend refresh catalog --json
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

Diagnostics include a provider-neutral `source_limits` section with availability or cooldown
state, observation and retry times, whether the retry time is exact, consecutive limit count, and
the latest refresh request and pause counts. It does not include response bodies, headers, or
credentials.

Use MCP to make watchlist and inbox state changes. The CLI deliberately exposes only list and show
operations for those records.

## Import Spotify listening history

Request your extended streaming history from Spotify's privacy settings and wait for the ZIP.
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

## Optional schedule

Music Friend does not install a schedule by default. A schedule runs `music-friend refresh all`
every six hours and exits. It requires an approved native credential store; the interactive vault
cannot be used for scheduled refreshes.

```bash
music-friend schedule status --json
music-friend schedule install
music-friend schedule remove
```

The command uses the current supported platform’s per-user scheduler: LaunchAgent on macOS or a
systemd user timer on Linux. Review the rendered platform entry after installation and remove it
when it is no longer wanted.

## Optional skill

`music-friend skill install` copies the packaged runtime skill into a client's skills directory.
See the [install guide](install.md#optional-agent-skill) for destinations and replacement rules.
