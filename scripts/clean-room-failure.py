#!/usr/bin/env python3
"""Extract a bounded, allowlisted clean-room failure summary."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

_TEST_LINE = re.compile(
    r"^(?:FAILED|ERROR) "
    r"(tests/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*\.py::[A-Za-z0-9_.\[\]-]+)"
    r"(?:\s+-|$)"
)
_STAGE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_MAX_FAILURES = 10
_MAX_LOG_BYTES = 1_048_576
_MAX_RECEIPTS = 100


def pytest_failures(path: Path) -> list[str]:
    """Return only validated repository-relative failed test identifiers."""
    try:
        if path.stat().st_size > _MAX_LOG_BYTES:
            return []
        lines = path.read_text(encoding="utf-8", errors="strict").splitlines()
    except (OSError, UnicodeError):
        return []
    identifiers = {match.group(1) for line in lines if (match := _TEST_LINE.match(line))}
    return sorted(identifiers)[:_MAX_FAILURES]


def nested_failure_stages(root: Path) -> list[str]:
    """Return validated stage names from bounded nested certification receipts."""
    try:
        receipts = sorted(root.glob("**/success.json"))[: _MAX_RECEIPTS + 1]
    except OSError:
        return []
    if len(receipts) > _MAX_RECEIPTS:
        return []
    stages: set[str] = set()
    for path in receipts:
        try:
            if path.stat().st_size > 16_384:
                continue
            payload = json.loads(path.read_text(encoding="utf-8", errors="strict"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if (
            isinstance(payload, dict)
            and payload.get("certification") == "development-clean-room"
            and payload.get("status") == "fail"
            and isinstance(payload.get("commit"), str)
            and _COMMIT.fullmatch(payload["commit"])
            and isinstance(payload.get("failed_stage"), str)
            and _STAGE.fullmatch(payload["failed_stage"])
        ):
            stages.add(payload["failed_stage"])
    return sorted(stages)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pytest-log", type=Path, required=True)
    parser.add_argument("--pytest-root", type=Path, required=True)
    arguments = parser.parse_args()
    print(
        json.dumps(
            {
                "failed_tests": pytest_failures(arguments.pytest_log),
                "nested_failed_stages": nested_failure_stages(arguments.pytest_root),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
