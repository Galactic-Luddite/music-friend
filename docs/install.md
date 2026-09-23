# Install

Music Friend requires Python 3.10 or newer. It runs on your computer and creates no account,
schedule, or network listener during installation.

Music Friend v0.1 supports macOS and Linux. Windows is not a supported runtime in v0.1.

## Before you start

Gather these once so setup never stops halfway:

- **Python 3.10 or newer.**
- **A native credential store.** macOS Keychain works out of the box. On Linux, install and unlock
  Secret Service (for example `gnome-keyring`) or KWallet. The MCP server and scheduled refreshes
  require it; without one, only interactive CLI commands work, through a passphrase vault.
- **A Spotify developer application** with a public client ID, PKCE enabled, and the exact redirect
  URI `http://127.0.0.1/callback` (see [setup](setup.md)).
- **Optional: a Ticketmaster Discovery API key** and an event area (country code, postal code, and
  radius) if you want concert discovery.

After installing, `music-friend doctor` checks all of these in one local report and names the fix
for anything missing. It never contacts a provider or prints a credential value.

## Intel macOS

No PyPI wheel is available for the Intel macOS build of the required `cryptography` 50
dependency. Build it from source before installing the release file. This needs Xcode command-line
tools, Rust 1.83 or newer, and a non-Apple OpenSSL; the OpenSSL supplied with macOS is unsupported
by cryptography.

```bash
xcode-select --install
brew install openssl@3 rust
rustc --version
OPENSSL_DIR="$(brew --prefix openssl@3)" \
  python -m pip install --no-binary cryptography music_friend-0.1.1-py3-none-any.whl
```

Confirm that `rustc --version` reports Rust 1.83 or newer. The command above dynamically links
the Homebrew OpenSSL. To build cryptography statically instead, use this install command:

```bash
OPENSSL_STATIC=1 \
  python -m pip install --no-binary cryptography music_friend-0.1.1-py3-none-any.whl
```

On platforms with a supported cryptography wheel, use the regular release-file installation below.

## Optional Agent Skill

MCP schemas remain the universal interface. Compatible clients can also install Music Friend's
optional packaged guidance after installing the wheel:

```bash
music-friend skill install --client codex
music-friend skill install --client claude
music-friend skill install --target SKILLS_DIRECTORY
```

The Codex command writes to `$HOME/.agents/skills/music-friend/SKILL.md`; the Claude command writes
to `$HOME/.claude/skills/music-friend/SKILL.md`. A custom skills directory receives
`music-friend/SKILL.md`. Repeating an installation with matching content makes no change. If the
destination has different content, inspect it before explicitly replacing it:

```bash
music-friend skill install --target SKILLS_DIRECTORY --replace
```

The installer reads only the skill packaged in the local Music Friend installation. It does not
contact a provider, use credentials, or configure MCP for the client.
Installation requires secure directory-relative filesystem operations that do not follow links.
On platforms without those operations, the command stops without writing instead of using a
path-based fallback.

After installation, run `music-friend doctor` and resolve anything it reports, then continue with
the [quickstart](quickstart.md). `music-friend status --json` is a safe local check; it reports
Spotify as disconnected until you connect it, and its `mcp_ready` field is `false` when no native
credential store is available for the MCP server.
