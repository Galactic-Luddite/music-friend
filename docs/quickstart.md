# Quickstart

Use this path to install Music Friend, connect Spotify, and make a first local refresh.

## 1. Install

Follow the [install guide](install.md), then confirm the command is available:

```bash
music-friend version
```

## 2. Connect Spotify

Create a Spotify developer application and register its current redirect URI. Then enter the
application client identifier through the local prompt and complete the browser authorization:

```bash
music-friend setup
music-friend connect spotify
music-friend status --json
```

Music Friend uses a public client identifier and PKCE. Do not create or enter a Spotify client
secret. See [setup](setup.md) for permissions, credential storage, and optional event discovery.

## 3. Refresh your catalog

```bash
music-friend refresh catalog --json
music-friend watchlist list --json
music-friend inbox list --json
```

A refresh reads Spotify data over the network, then writes the result only to your local catalog;
it never changes your Spotify account. A refresh can report a partial result when a provider limit
prevents completion; see [provider limits](limits.md) for what that means.

## 4. Optionally connect an MCP client

After setup is complete, configure a local MCP client with
`music-friend-mcp`. The [MCP guide](mcp.md) includes ready-to-copy client entries.

For a problem with installation, setup, or a refresh, use [troubleshooting](troubleshooting.md).
