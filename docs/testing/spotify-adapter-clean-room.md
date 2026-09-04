# Spotify adapter development clean room

This command verifies one clean, exact commit in an isolated local environment. It exports the
tracked source, installs all build, test, and runtime dependencies from an explicit offline
wheelhouse, runs the executable test and analysis gates, builds a wheel and source distribution,
installs the wheel in a second environment, and binds the resulting evidence to file digests.

## Prerequisites

The checkout must be clean at the full commit supplied to the command. Supply an absolute,
non-symbolic-link Python 3.10 executable, an absolute non-symbolic-link directory containing only
the required wheel files, and an absent result path whose existing parent is outside the checkout.
The wheelhouse must contain the complete build, development, and runtime dependency graph.

## Command

```bash
python scripts/clean-room-spotify.py \
  --source-commit 0123456789abcdef0123456789abcdef01234567 \
  --python /absolute/path/to/python3.10 \
  --wheelhouse /absolute/path/to/wheelhouse \
  --result /absolute/path/to/result.json
```

The command creates the result once with mode `0600`. It does not use a package index. Temporary
state is removed after success and retained for local diagnosis after failure.

## Evidence

A passing result is bounded JSON labeled `development-clean-room`. Its top-level fields are:

- `archive_sha256`
- `artifacts`
- `boundaries`
- `certification`
- `commands`
- `commit`
- `interpreter`
- `positive_controls`
- `scans`
- `status`
- `wheelhouse_sha256`

The executable schema tests define these fields. The boundary evidence reports mocked Spotify
hosts separately from real socket operations, records callback lifecycle events, and must show no
real bind, connect, or send. Scan results cover the source archive, exported tree, both built
artifacts, and isolated state categories. A passing result applies only to the exact recorded
commit, interpreter digest, wheelhouse digest, archive digest, wheel digest, and source-distribution
digest.

## Development-only scope

Only macOS Keychain credential storage is verified by this development-only command.
Windows credential storage is not verified by this command.
Linux credential storage is not verified by this command.
These statements do not make a broader platform compatibility claim.

All HTTP behavior in this command uses scripted mocks. It does not authorize a Spotify account,
open a real browser, bind a real callback socket, or access a real credential store.
Live certification and release certification are separate and require their own authorization and
evidence. This document describes development verification only.
