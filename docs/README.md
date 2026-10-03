# Documentation

Pick the goal that matches what you want to do.

## Get started

- [Install](install.md) — installation choices, Intel macOS build notes, and the optional skill
- [Quickstart](quickstart.md) — install, connect Spotify, and fetch recent listening observations
- [Spotify and optional event setup](setup.md) — developer application, redirect URI, credential
  storage, and the Ticketmaster event area

## Use an AI client

- [MCP clients](mcp.md) — start the local stdio server, copy a client entry, and learn what each of
  the eleven tools read, change, or fetch

## Use my listening history

- [Quickstart](quickstart.md) — start ongoing recent-listening sync
- [CLI and data operations](operations.md#import-spotify-listening-history) — optionally backfill
  older listening from a Spotify extended streaming-history ZIP
- [MCP clients](mcp.md#listening-history-evidence) — ask a bounded history question

## Manage or erase data

- [CLI and data operations](operations.md#export-backup-restore-and-deletion) — export, back up,
  restore, and delete local data; remove provider credentials
- [CLI and data operations](operations.md#daily-schedule) — install or remove an optional refresh
  schedule; history-only jobs run every six hours and other kinds run daily

## Understand results

- [Provider limits](limits.md) — freshness windows, partial results, cooldowns, and manual checks
- [Security and privacy](security.md) — what stays local, credential handling, and safe reporting

## Troubleshoot

- [Troubleshooting](troubleshooting.md) — command not found, disconnected Spotify, partial
  refreshes, unavailable events, and what to remove before sharing output

## Contribute

- [Contributing](../CONTRIBUTING.md) — issue-first changes, synthetic data, and focused diffs
- Repository contributor guidance at the checkout root — source map, boundaries, validation
  commands, and public-repository safety rules for people and coding agents
- [Product design](design/product-design.md) — the v0.1.0 boundary and tool surface
- [Spotify adapter clean room](testing/spotify-adapter-clean-room.md) — the isolated verification
  command and its evidence

Examples use synthetic values. Keep credentials, personal listening data, exports, databases, and
machine-specific paths out of reports and copied command output.
