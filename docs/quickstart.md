# Quickstart

Use this path to install Music Friend and fetch recent Spotify listening observations. Catalog,
release, event, archive-import, and MCP setup can wait until this first result works.

## 1. Install

[Install `uv`](https://docs.astral.sh/uv/getting-started/installation/), install Music Friend, and
then confirm the command is available. If `music-friend` is not found, follow the `uv` installer's
PATH instructions and open a new terminal.

```bash
uv tool install --from git+https://github.com/Galactic-Luddite/music-friend.git music-friend
music-friend version
```

## 2. Connect Spotify

Create your own Spotify developer application and register
`http://127.0.0.1/callback` as a redirect URI. Replace `YOUR_CLIENT_ID` below with its public client
ID, then complete browser authorization:

```bash
music-friend setup --spotify-client-id YOUR_CLIENT_ID --json
music-friend connect spotify
```

Music Friend uses a public client identifier and PKCE. Do not create or enter a Spotify client
secret. The callback listens temporarily on `127.0.0.1` during authorization and closes when the
connection finishes. Existing users must run `music-friend connect spotify` again once to grant
the `user-read-recently-played` permission.

## 3. Fetch recent listening observations

```bash
music-friend refresh history --json
```

The first check stores only the recent plays Spotify currently returns. Spotify does not document
a complete retention window, so this is a starting snapshot rather than a guaranteed complete
history. Future checks append new observations. To backfill older listening, optionally import a
new Spotify extended streaming-history archive; see
[history import](operations.md#import-spotify-listening-history).

## 4. Check local readiness

```bash
music-friend doctor
music-friend status --json
```

`doctor` names any remaining local prerequisite. A failed `credential_store` check means scheduled
refreshes and the MCP server cannot run yet, although interactive CLI use can use its passphrase
vault.

## 5. Optionally keep it current

Install a history-only job that runs every six hours on a computer that will be running at the
scheduled time:

```bash
music-friend schedule install --kind history
```

The job avoids catalog, release, and event work. Six-hour polling reduces gaps but cannot guarantee
none: Spotify's recent-play retention is undocumented and listening between polls can exceed what
the bounded request retrieves.

## 6. Optionally connect an MCP client

After setup is complete, configure a local MCP client with
`music-friend-mcp`. The [MCP guide](mcp.md) includes ready-to-copy client entries.

Then ask your assistant to refresh listening history and summarize recent listening. Ask it to
check `music_status.history.last_successful_check_at` and coverage facts before describing the
answer as current.

For a problem with installation, setup, or a refresh, use [troubleshooting](troubleshooting.md).
