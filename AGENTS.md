# Contributor guidance

This file is the cold-start contract for anyone, person or coding agent, who opens this source
checkout. Read it before changing code, tests, or documentation.

Music Friend is a local-first music companion. It keeps music data on the person's computer and
provides a provider-neutral interface for local discovery, watchlists, inboxes, and imported
listening history. Do not add a hosted account, network listener, telemetry, ticket purchasing, or
a provider-specific MCP tool (see: docs/design/product-design.md).

## Source map

- `src/music_friend/runtimes/cli.py` is the `music-friend` command. It owns the `doctor` preflight, setup, credentials,
  refreshes, data lifecycle, schedules, and skill installation (see: docs/operations.md).
- `src/music_friend/runtimes/mcp_stdio.py` is the `music-friend-mcp` command. It runs the catalog
  server from `src/music_friend/mcp/catalog_server.py`, whose `_TOOL_SCHEMAS` mapping is the
  canonical list of the nine advertised MCP tools (see: docs/mcp.md).
- `src/music_friend/tools/` holds the application layer (`MusicFriendApplication`) and the refresh
  flow that both runtimes call (see: tests/tools/).
- `src/music_friend/store/` is the SQLite catalog, schema migrations, and the Spotify history
  importer (see: tests/store/).
- `src/music_friend/providers/` contains the Spotify and Ticketmaster adapters, transports, and
  credential stores. Provider code never imports MCP or runtime modules (see: tests/architecture/test_dependencies.py).
- `src/music_friend/domain/` defines the provider-neutral models and enums (see: tests/domain/).
- `skills/music-friend/` is the optional runtime skill packaged unchanged in the wheel (see: docs/install.md).
- `tests/` mirrors the source layout and adds `tests/docs/` (public documentation contracts),
  `tests/architecture/` (import boundaries), `tests/security/`, and `tests/clean_room/`.
- `docs/` is the user-facing documentation; `docs/README.md` is its goal-oriented index.
- `scripts/` holds the public-tree scanner, clean-install check, and clean-room commands used by
  CI (see: docs/testing/spotify-adapter-clean-room.md).

## Architectural boundaries

- The catalog's local identifiers are the public identity. Provider identifiers stay behind
  adapters and never appear at the MCP boundary (see: tests/mcp/test_catalog_server.py).
- MCP tools read and change local state only, except `refresh_music`, which contacts a provider read-only (see: docs/mcp.md).
  MCP never accepts credentials or performs imports, restores, backups, schedules, or deletion;
  those are CLI operations (see: docs/operations.md).
- Imported listening history is evidence, not preference. It does not feed watchlist affinity
  (see: docs/design/product-design.md).
- Refreshes are bounded, one-shot, and record partial outcomes instead of failing silently (see: docs/limits.md).
- `tests/architecture/test_dependencies.py` enforces the import direction; a change that needs a
  new edge needs a design discussion first.

## Sources of truth

- MCP tool names and schemas: `_TOOL_SCHEMAS` in `src/music_friend/mcp/catalog_server.py`.
  `docs/mcp.md`, the skill, and `tests/docs/test_public_contract.py` must match it.
- CLI grammar: `_USAGE` in `src/music_friend/runtimes/cli.py`; `docs/operations.md` documents it
  and `tests/docs/test_public_contract.py` allows only real commands in examples.
- CI gates: `.github/workflows/local-validation.yml`; `tests/docs/test_ci_definition.py` and the
  validation commands below must stay in step with it.
- Supported platforms and Python range: `pyproject.toml` and `docs/install.md`.
- Product boundary: `docs/design/product-design.md`.

## Supported Python versions

Support Python 3.10 through 3.14. Keep compatibility with the supported range when changing
runtime code or package metadata.

## Implementation workflow

1. Start from an issue that names the user-visible outcome; reference it in the change.
2. Read the relevant source and its tests before editing. Keep the diff focused on that issue.
3. Cover behavior changes and failure cases with tests, using synthetic data only.
4. Update documentation in the same change when a command, MCP tool, schema, data format,
   validation command, or boundary changes. Update `CHANGELOG.md` for user-visible changes.
5. Run the fast validation while iterating and the full validation before proposing the change.

## Development environment

Use a virtual environment; current Debian and Ubuntu refuse installs into the system Python
(PEP 668):

```bash
python -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/music-friend doctor
```

Run the commands below with `.venv/bin/python`, or activate the environment first. `doctor` checks
Python, the native credential store, and provider configuration locally; it never contacts a
provider. Use synthetic configuration; never connect a real account while developing.

`tests/agent_skill/test_install.py::test_wheel_contains_the_exact_canonical_skill_and_installs_it`
installs the built wheel offline and needs `MF_PHASE1_TEST_WHEELHOUSE` pointing at a wheelhouse of
the runtime dependencies; without it the test fails locally and passes in CI.

## Validation

Fast check while iterating:

```bash
python -m pytest -q --ignore=tests/clean_room
python -m ruff check src tests scripts
```

Full local gate, matching CI:

```bash
python -m pytest -q
python -m ruff check src tests scripts
python -m ruff format --check src tests scripts
python -m mypy --strict src/music_friend
python scripts/scan_public_tree.py .
```

CI additionally runs `pip_audit`, `piplicenses`, a Gitleaks history scan, a wheel build with a
public-tree scan of `dist`, a clean install from an offline wheelhouse, and the clean-room job in
`scripts/clean-room-phase1.sh` (see: tests/docs/test_ci_definition.py).

## Documentation responsibilities

- `README.md` is the front door: outcomes first, then the flow, first-success path, and links (see: tests/docs/test_public_contract.py).
- `docs/mcp.md` describes every advertised tool with purpose, inputs, result, and whether it is
  read-only, writes locally, or contacts a provider (see: tests/docs/test_public_contract.py).
- `docs/operations.md` documents the CLI by command group.
- Keep facts in one place and link to them; do not duplicate the provider-limit or privacy material (see: docs/limits.md).
- `tests/docs/` holds executable documentation contracts. Extend them when adding a fact that is
  likely to drift.

## Public-repository safety

This repository is public. Use synthetic data in tests, examples, issue reports, and copied command
output. Never include credentials, personal listening data, exports, local databases, or
machine-specific paths. `python scripts/scan_public_tree.py .` rejects credential-shaped strings,
private paths, and local databases; the public documentation tests reject private process residue.

## Provider boundaries

Keep provider behavior behind adapters and preserve local catalog identifiers at the public tool
boundary. New provider capabilities need explicit permissions, attribution, terms review, local
data-lifecycle coverage, and tests. Keep the MCP surface provider-neutral.
