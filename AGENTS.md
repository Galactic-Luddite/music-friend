# Contributor guidance

Music Friend is a local-first music companion. It keeps music data on the person's
computer and provides a provider-neutral interface for local discovery, watchlists, and inboxes.
Do not add a hosted account, network listener, ticket purchasing, or a provider-specific MCP tool.

## Source layout

- `src/music_friend/` contains the package and its local CLI and MCP entry points.
- `tests/` contains executable behavior and public-contract checks.
- `docs/` contains user-facing setup, operations, privacy, and MCP guidance.
- `skills/music-friend/` contains the optional runtime skill distributed with the package.

## Supported Python versions

Support Python 3.10 through 3.14. Keep compatibility with the supported range when changing
runtime code or package metadata.

## Validation

Run the relevant checks before proposing a change:

```bash
python -m pytest -q
python -m ruff check src tests scripts
python -m ruff format --check src tests scripts
python -m mypy --strict src/music_friend
python scripts/scan_public_tree.py .
```

Use synthetic data in tests, examples, issue reports, and copied command output. Never include
credentials, personal listening data, exports, local databases, or machine-specific paths.

## Provider boundaries

Keep provider behavior behind adapters and preserve local catalog identifiers at the public tool
boundary. New provider capabilities need explicit permissions, attribution, terms review, local
data-lifecycle coverage, and tests. Keep the MCP surface provider-neutral.
