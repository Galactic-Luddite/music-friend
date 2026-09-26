# Spotify, release source, and optional event setup

## Release source

Music Friend uses MusicBrainz for release discovery by default. It needs no API key and maps
watched artists to MusicBrainz identities during `music-friend refresh releases`. Use Spotify as
the release source only when its release catalog is preferable for your account:

```bash
music-friend setup --release-source musicbrainz
music-friend setup --release-source spotify
```

Interactive setup also asks for the release source; an empty response selects MusicBrainz unless a
previous selection is already saved.

## Spotify

Create a Spotify developer application using Spotify’s current
[developer dashboard](https://developer.spotify.com/dashboard) instructions. Music Friend uses a
public client identifier and PKCE. Do not create, paste, or store a Spotify client secret for this
application.

Run the local setup command and enter the application client identifier at its interactive prompt:

```bash
music-friend setup
music-friend connect spotify
music-friend status --json
```

Register this exact redirect URI with Spotify: `http://127.0.0.1/callback`. During a connection,
Music Friend opens a temporary loopback listener on an available local port, so the browser uses a
callback such as `http://127.0.0.1:49152/callback` for that connection. Verify Spotify’s current
redirect-URI policy when registering the application. Music Friend requests read scopes for
profile, followed artists, saved music, and top artists. It does not ask for a Spotify password in
the CLI or MCP conversation.

The app prefers an approved operating-system credential store. If one is unavailable, interactive
use can fall back to a passphrase-protected local vault. This fallback is interactive CLI only; the
current stdio MCP server and schedule commands require an approved native credential store.
Setup does not install a refresh schedule. To enable a daily per-user schedule explicitly, use
`music-friend schedule install` on a computer intended to run scheduled jobs.
Disconnecting removes the locally stored Spotify credential:

```bash
music-friend disconnect spotify
```

## Ticketmaster event discovery

Ticketmaster is the only event-discovery adapter in this version. Event refresh requires a
user-owned Ticketmaster Discovery API key and an explicitly configured home search area. Run
`music-friend setup` again to enter or change the country code, postal code, radius, unit, and key.
The key uses a hidden interactive prompt and is stored through the local credential boundary, not
in the catalog or configuration file. Leave a prompt blank to preserve its current value; enter
`-` at any event-area prompt to clear the complete event area, or at the key prompt to remove the
stored key.

Use `music-friend status --json` to check the local `events.ready` field before an event refresh.
The default event radius is 50 miles or 80 kilometers when the selected unit has no saved radius.
The setup command rejects an incomplete event area.

Event discovery is read-only. It can retain source and purchase links for a person to open, but it
never buys tickets or automates a provider account.
