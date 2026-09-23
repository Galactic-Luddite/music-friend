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

## Event discovery is unavailable

Run `music-friend setup` to review the Ticketmaster key and complete home search area. A country
and postal code are required before an event refresh. [Event setup](setup.md) describes the area
and radius fields.

## Before sharing output

Diagnostics, exports, and backups can include personal catalog data. Remove personal data before
sharing anything, and never share a provider credential. See [security and privacy](security.md).
