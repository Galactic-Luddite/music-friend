# Contributing

Thank you for improving Music Friend. Start with the repository contributor guidance at the
checkout root: it is the cold-start contract for people and coding agents, with the source map,
architectural boundaries, validation commands, and public-repository safety rules. Then discuss
the change in an issue before proposing it.

## Privacy

Use synthetic data in tests, examples, screenshots, issues, and pull requests. Do not submit provider credentials, personal listening histories, exports, local databases, email or calendar content, machine-specific paths, or private infrastructure details.

## Changes

- Reference the issue in each change and keep each change focused on one outcome (see: docs/README.md).
- Keep provider-specific behavior behind adapter boundaries (see: tests/architecture/test_dependencies.py).
- Cover behavior changes and failure cases with tests (see: tests/).
- Update user-facing documentation when commands, MCP tools, or data formats change (see: tests/docs/test_public_contract.py).
- Explain new provider permissions and why each is needed.
- Preserve attribution and comply with third-party licenses and terms.

## Validation

The fast check while iterating:

```bash
python -m pytest -q --ignore=tests/clean_room
python -m ruff check src tests scripts
```

The full local gate, matching CI:

```bash
python -m pytest -q
python -m ruff check src tests scripts
python -m ruff format --check src tests scripts
python -m mypy --strict src/music_friend
python scripts/scan_public_tree.py .
```

Keep changes focused and describe the user-visible result.

## Versioning

Music Friend follows `0.MINOR.PATCH` until 1.0, using PEP 440 version strings.

- **Patch** (`0.2.1`): bug fixes that leave MCP result shapes, CLI output, and exit codes unchanged.
- **Minor** (`0.3.0`): anything a client can observe, including new result fields, new status
  values, changed matching or defaults, and schema migrations.
- **Release candidates** (`0.3.0rc1`) for trial installs; pip ignores them unless `--pre` is used.
  Do not use suffixes such as `0.2.1a`, which PEP 440 sorts before `0.2.1`.

Add user-visible changes under `## Unreleased` in `CHANGELOG.md`; a release PR renames that heading
to the new version and bumps `pyproject.toml`.
