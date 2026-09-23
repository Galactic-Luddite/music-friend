# Changelog

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
