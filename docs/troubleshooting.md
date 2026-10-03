# Troubleshooting

## The command is not found

Confirm Music Friend was installed with the same Python environment that supplies your command
line. Reinstall with the [install guide](install.md), then run:

```bash
music-friend version
```

## Spotify is disconnected

Check local status, then repeat the interactive connection if needed:

```bash
music-friend status --json
music-friend connect spotify
```

Use the client identifier from your Spotify developer application. Do not enter a client secret.
Review [Spotify setup](setup.md) for redirect-URI and credential-store requirements.

## An MCP client cannot refresh music

Complete Spotify setup with the CLI first. The MCP server requires an approved native credential
store; the passphrase-protected vault works only with the interactive CLI. See the [MCP guide](mcp.md).
Run `music-friend doctor`: a failed `credential_store` check means this host has no approved
native store, and `music-friend status --json` reports `"mcp_ready": false`.

## A refresh is partial or unavailable

A provider limit or a saved cooldown can leave a refresh partial. Check the reported status and
diagnostics:

```bash
music-friend diagnostics --json
```

See [provider limits](limits.md) for how refreshes record cooldowns and resume later.

If `history.outcome` is `permission_required`, reconnect Spotify explicitly to grant the recently
played permission. This is required once for connections created before recent-history sync was
added. `cooling_down`, `quota_exhausted`, `bounded_partial`, and `failed` remain visible
independently of the overall refresh result. Local status and summaries never test the connection
or refresh a token. Backup and restore preserve API observations, sync state, and incomplete
intervals; `data purge spotify` removes them, while disconnecting Spotify retains local evidence.

## Recent history has gaps

A successful refresh means Music Friend completed its bounded check; it does not mean Spotify
returned a complete listening history. Keep the six-hour history-only schedule installed to reduce
future gaps. For older or missing periods, request a new Spotify extended streaming-history archive
and use the validate-then-import flow in [CLI and data operations](operations.md#import-spotify-listening-history).

## Event discovery is unavailable

Run `music-friend setup` to review the Ticketmaster key and complete home search area. A country
and postal code are required before an event refresh. [Event setup](setup.md) describes the area
and radius fields.

## Before sharing output

Diagnostics, exports, and backups can include personal catalog data. Remove personal data before
sharing anything, and never share a provider credential. See [security and privacy](security.md).
