# CLI and data operations

Run `music-friend --help` for the complete command grammar. Commands that inspect or refresh local
state support `--json` as their final argument.

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

Diagnostics include a provider-neutral `source_limits` section with availability or cooldown
state, observation and retry times, whether the retry time is exact, consecutive limit count, and
the latest refresh request and pause counts. It does not include response bodies, headers, or
credentials.

Use MCP to make watchlist and inbox state changes. The CLI deliberately exposes only list and show
operations for those records.

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

The command uses the current platform’s per-user scheduler: LaunchAgent on macOS, Task Scheduler
on Windows, or a systemd user timer on Linux. Review the rendered platform entry after installation
and remove it when it is no longer wanted.
