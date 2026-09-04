# Install

Music Friend requires Python 3.10 or newer. It runs on your computer and creates no account,
schedule, or network listener during installation.

Music Friend v0.1 supports macOS and Linux. Windows is not a supported runtime in v0.1.

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
  python -m pip install --no-binary cryptography music_friend-0.1.0-py3-none-any.whl
```

Confirm that `rustc --version` reports Rust 1.83 or newer. The command above dynamically links
the Homebrew OpenSSL. To build cryptography statically instead, use this install command:

```bash
OPENSSL_STATIC=1 \
  python -m pip install --no-binary cryptography music_friend-0.1.0-py3-none-any.whl
```

On platforms with a supported cryptography wheel, use the regular release-file installation below.

## From a release file

Download the release file for Music Friend, then install it with pip:

```bash
python -m pip install music_friend-0.1.0-py3-none-any.whl
music-friend version
music-friend --help
```

For an isolated command installation, use pipx with the same file:

```bash
pipx install music_friend-0.1.0-py3-none-any.whl
music-friend version
```

## From a source checkout

```bash
python -m pip install .
music-friend version
```

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

After installation, continue with the [quickstart](quickstart.md). `music-friend status --json`
is a safe local check; it reports Spotify as disconnected until you connect it.
