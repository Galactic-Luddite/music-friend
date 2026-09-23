# Music Friend

Music Friend is an **AI-agent-first** music companion that runs on your own computer. There is no
app, GUI, or TUI to learn: you ask your AI assistant (Claude, Codex, or any MCP-capable client, in
the terminal or the desktop apps) and it uses Music Friend's tools to answer with real data about
*your* music.

> "Anything new from the artists I follow this week?"
> "Any of them playing within 50 miles next month?"
> "What did I listen to most in March, and who fell off my rotation?"

Behind those answers, Music Friend keeps a local catalog, a watchlist of artists you care about,
an inbox of new releases and nearby shows (each with the reason it appeared), and your imported
Spotify listening history. They are stored together in one SQLite file on your machine; provider
credentials live in your operating-system credential store.

## Is this for you?

Music Friend is a power-user tool. Before it is useful you need:

- **A computer that stays yours.** Everything runs locally: the `music-friend-mcp` server your AI
  client launches, the local catalog, and (if you opt in) a once-a-day refresh job. Nothing is
  hosted; there is no Music Friend account or cloud service.
- **An AI client that speaks MCP**, such as Claude Code, Claude Desktop, or Codex. The optional
  packaged skill teaches the assistant how to use the tools well (see [install](docs/install.md)).
- **Your own Spotify developer application.** Spotify does not offer a shared public app for tools
  like this, so every user registers a free developer app and uses its public client ID
  ([setup](docs/setup.md)). Ticketmaster event discovery is optional and needs its own free key.
- **Comfort with a terminal** for one-time setup: install, `music-friend setup`,
  `music-friend connect spotify`, and `music-friend doctor` to confirm everything is ready.

After that, your assistant does the work. Refreshes run when the assistant (or you) asks, or once a
day if you install the optional per-user schedule with `music-friend schedule install`.

What your assistant can do with it:

- **Build a local catalog** from the artists you follow and the music you have saved on Spotify.
- **Watch artists** by adding, pinning, or muting them so that discovery follows your choices.
- **Triage new releases and nearby events** in a local inbox, with a stored explanation for why
  each item appeared. Event discovery through Ticketmaster is optional.
- **Answer questions about your listening history** after you import your Spotify extended
  streaming-history archive, such as "what did I play most in March?"

It has no Music Friend account, hosted catalog, resident daemon, telemetry, or ticket purchasing.
The MCP server runs only while your AI client is using it, and a scheduled refresh runs once and
exits.

## How it works

You talk to your AI client; it starts `music-friend-mcp` on your computer over stdio and calls
its tools. Those tools read and update the local data below.

```text
Spotify account ----(read-only refresh)----+
Ticketmaster (optional) --(read-only)------+--> local catalog (SQLite) --> watchlist --> inbox
Spotify history ZIP -----(local import)----+                          \--> history answers
```

1. **Input.** A refresh reads your followed artists, saved music, and top artists from Spotify,
   and optionally upcoming events from Ticketmaster. A history import reads a Spotify extended
   streaming-history ZIP that you downloaded yourself.
2. **Local catalog.** Results are normalized and written to your local catalog. Music Friend
   never writes to your Spotify or Ticketmaster account.
3. **Watchlist.** The catalog decides which artists to monitor; your explicit add, pin, mute, and
   remove decisions override it and persist across refreshes.
4. **Inbox.** New releases and matching events become inbox items that you can read, save, or
   dismiss. Each item keeps the reason it was included.
5. **History answers.** Imported plays stay separate from preferences and are summarized only for
   the date range you ask about.

Music Friend contacts a provider only when you run a refresh or connect an account. Reading the
catalog, watchlist, inbox, or history never leaves your computer.

## First success in six commands

After [installing](docs/install.md) and creating a Spotify developer application
([setup guide](docs/setup.md)):

```bash
music-friend doctor
music-friend setup
music-friend connect spotify
music-friend refresh catalog --json
music-friend watchlist list --json
music-friend inbox list --json
```

`doctor` lists anything still missing, with its fix, before you start. The
[quickstart](docs/quickstart.md) walks through each step. Refreshes can report a partial
result when a provider limit interrupts them; see [provider limits](docs/limits.md).

## Follow an artist through the inbox

Connect an [MCP client](docs/mcp.md) with the `music-friend-mcp` command, then ask it to:

1. `search_catalog` for the artist by name and confirm the local identifier.
2. `update_watchlist` with that identifier and the action `pin`.
3. `refresh_music` with kind `releases` (or `events` once an event area is configured).
4. `list_inbox` filtered to `unread`, then `explain_inbox_item` to see why an item appeared.
5. `update_inbox_item` to mark it `saved` or `dismissed`.

The [MCP guide](docs/mcp.md#tool-surface) explains each of the nine tools, what it reads or
changes, and when it contacts a provider.

## Import and query listening history

Request your extended streaming history from Spotify, then validate and import the ZIP locally:

```bash
music-friend data import-spotify my_spotify_data.zip --dry-run --json
music-friend data import-spotify my_spotify_data.zip --json
```

The importer keeps music plays only. It ignores podcasts, audiobooks, and video, and it never
stores IP address, device, or connection-country fields. Afterwards an MCP client can call
`summarize_listening_history` with a UTC date range to get play counts, listening time, and top
artists and tracks for that period. See [CLI and data operations](docs/operations.md#import-spotify-listening-history).

## Privacy at a glance

- **Local storage.** The catalog, watchlist, inbox, and imported history are SQLite data in your
  user data directory. There is no hosted copy.
- **Credentials.** Spotify uses a public client identifier with PKCE; there is no client secret.
  Tokens and the optional Ticketmaster key go to your operating-system credential store (or an
  interactive passphrase vault) and are never accepted through MCP.
- **No telemetry, no service.** Music Friend sends no telemetry and has no hosted service. It
  contacts only the providers you configure, during connection or a refresh you request, and
  listens on no network port except a temporary loopback callback during Spotify authorization.
- **History imports** exclude IP address, device, and connection-country fields.
- **Exports and backups** are personal data. They exclude credentials but include your catalog and
  history; store them where you control access.

Read [security and privacy](docs/security.md) for the full policy and
[SECURITY.md](SECURITY.md) to report a vulnerability.

## Documentation

| Goal | Start here |
|------|------------|
| Install and get a first result | [Install](docs/install.md), [Quickstart](docs/quickstart.md) |
| Connect Spotify or configure events | [Spotify and event setup](docs/setup.md) |
| Use Music Friend from an AI client | [MCP clients](docs/mcp.md) |
| Run refreshes, import history, export, back up, erase, or schedule | [CLI and data operations](docs/operations.md) |
| Understand freshness and partial results | [Provider limits](docs/limits.md) |
| Fix a setup or refresh problem | [Troubleshooting](docs/troubleshooting.md) |
| Know what stays local | [Security and privacy](docs/security.md) |
| Contribute a change | [Contributing](CONTRIBUTING.md) and the repository contributor guidance at the checkout root |

The [documentation index](docs/README.md) lists every guide by goal. The
[product design](docs/design/product-design.md) records the v0.1.0 boundary.

## Contributing and security

Open an issue before a substantial change, use synthetic data everywhere, and keep credentials and
real listening history out of public artifacts. [CONTRIBUTING.md](CONTRIBUTING.md) summarizes the
rules; the repository contributor guidance at the checkout root is the cold-start contract for
people and coding agents working in a source checkout. Report vulnerabilities through
[SECURITY.md](SECURITY.md).

Read the [changelog](CHANGELOG.md) or report a reproducible problem through the
[issue tracker](https://github.com/Galactic-Luddite/music-friend/issues).

## Attribution

Some Spotify authorization and request patterns are modified from
[Spotify Skill](https://github.com/fabioc-aloha/spotify-skill), licensed under Apache-2.0. This
credit does not imply that the upstream project endorses Music Friend.
