# Music Friend

Music Friend is a local-first music companion. It keeps a catalog, watchlist, and inbox on your
computer for Spotify release discovery and optional Ticketmaster event discovery. Use it through
the local CLI or a small provider-neutral MCP interface.

MCP schemas are the universal integration interface. The optional runtime skill adds concise
guidance for compatible skill clients; repository contributor guidance applies only inside a
source checkout.

It has no Music Friend account, hosted catalog, background daemon, ticket purchasing, or
provider-specific MCP tools. Refreshes are on demand by default; an optional schedule runs one
bounded refresh and exits.

## First run

1. [Install Music Friend](docs/install.md).
2. Follow the [quickstart](docs/quickstart.md) to connect Spotify and make the first catalog
   refresh. See [Spotify and event setup](docs/setup.md) for connection details.
3. Optionally configure an [MCP client](docs/mcp.md) for watchlist and inbox requests.

Use [troubleshooting](docs/troubleshooting.md) if a local command or connection needs attention.
Read [provider limits](docs/limits.md) before relying on discovery results, and [security and
privacy](docs/security.md) before sharing diagnostics or data. [Local operations](docs/operations.md)
cover exports, backups, and optional scheduling.

## Attribution

Some Spotify authorization and request patterns are modified from
[Spotify Skill](https://github.com/fabioc-aloha/spotify-skill), licensed under Apache-2.0. This
credit does not imply that the upstream project endorses Music Friend.

## Contributing and security

See [CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md). Repository contributor
guidance is available at the checkout root.
Read the [changelog](CHANGELOG.md), browse the [documentation](https://github.com/Galactic-Luddite/music-friend#readme),
or report a reproducible problem through the [issue tracker](https://github.com/Galactic-Luddite/music-friend/issues).
