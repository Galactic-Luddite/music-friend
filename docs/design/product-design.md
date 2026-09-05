# Music Friend v0.1.0 design

Music Friend is a local-first catalog and inbox for music discovery. Its working data is SQLite on
the user’s device. Spotify supplies the current music catalog and release path; Ticketmaster is the
optional event-discovery adapter. Neither provider identifier is the local catalog’s primary
identity.

The normal conversation surface is a provider-neutral MCP stdio server with nine bounded tools for
status, refresh, catalog search, watchlist inspection and update, inbox inspection and update,
inbox explanations, and listening-history summaries. Connection, credential storage, schedules,
import, restore, backup, and data deletion remain local CLI operations.

Refreshes are one-shot, bounded operations. They preserve completed local work when another
capability fails, record partial outcomes, and write release or event signals into a local inbox.
Watchlist decisions are explicit and durable: a person can add, pin, mute, or remove a local artist
override. Every inbox item retains a local explanation for why it appeared.

Listening history is evidence, not preference. A person imports their own Spotify extended
streaming-history archive through the local CLI; the importer validates the archive, keeps
music-track plays only, and never stores IP address, device, or connection-country fields. The MCP
surface can summarize imported plays for a UTC date range, and every summary names its evidence
boundary. Imported plays do not feed watchlist affinity or local preferences.

The v0.1.0 boundary excludes a hosted service, dashboard, mobile client, native installer, resident
daemon, ticket purchasing, managed provider login, additional providers, and a model runtime.
Provider capabilities and terms may change, so use the [limits guide](../limits.md) before relying
on a result.
