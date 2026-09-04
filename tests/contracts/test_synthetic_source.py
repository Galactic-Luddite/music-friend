"""Contract coverage for the deterministic synthetic music source."""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any, cast

import pytest

from music_friend.providers import Capability

from .source_contract import (
    DEFAULT_PROVIDER_TEXT,
    RECORD_OPERATION_CASES,
    MusicSourceContract,
    OperationCase,
    assert_adversarial_text_remains_data,
)
from .synthetic_source import SCENARIOS, SyntheticSourceFactory

FIXTURE_PATH = Path(__file__).parents[1] / "security" / "injection-fixtures" / "source_text.json"
SYNTHETIC_SOURCE_PATH = Path(__file__).with_name("synthetic_source.py")
PROHIBITED_IMPORT_ROOTS = {
    "aiohttp",
    "boto3",
    "http",
    "httpx",
    "os",
    "pathlib",
    "random",
    "requests",
    "secrets",
    "socket",
    "spotipy",
    "time",
}
PROHIBITED_ACCESS_SNIPPETS = (
    ("import socket", "import:socket"),
    ("open('synthetic')", "builtin:open"),
    ("__import__('synthetic')", "builtin:__import__"),
    ("eval('synthetic')", "builtin:eval"),
    ("exec('synthetic')", "builtin:exec"),
    ("datetime.now()", "clock:datetime.now"),
    ("datetime.utcnow()", "clock:datetime.utcnow"),
    ("datetime.today()", "clock:datetime.today"),
    ("date.today()", "clock:date.today"),
    ("time.time()", "clock:time.time"),
    ("time()", "clock:time"),
)


class TestSyntheticMusicSource(MusicSourceContract):
    source_factory = SyntheticSourceFactory(provider_text=DEFAULT_PROVIDER_TEXT)


def _fixture_cases() -> list[dict[str, Any]]:
    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    return cast(list[dict[str, Any]], fixture["cases"])


def _prohibited_accesses(source: str) -> tuple[str, ...]:
    tree = ast.parse(source)
    violations: set[str] = set()
    builtin_calls = {"__import__", "eval", "exec", "open"}
    clock_names = {
        "date": {"today"},
        "datetime": {"now", "today", "utcnow"},
    }

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".", 1)[0]
                if root in PROHIBITED_IMPORT_ROOTS:
                    violations.add(f"import:{root}")
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            root = node.module.split(".", 1)[0]
            if root in PROHIBITED_IMPORT_ROOTS:
                violations.add(f"import:{root}")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in builtin_calls:
                violations.add(f"builtin:{node.func.id}")
            elif node.func.id == "time":
                violations.add("clock:time")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            owner = node.func.value
            if isinstance(owner, ast.Name):
                if owner.id == "builtins" and node.func.attr in builtin_calls:
                    violations.add(f"builtin:{node.func.attr}")
                if node.func.attr in clock_names.get(owner.id, set()):
                    violations.add(f"clock:{owner.id}.{node.func.attr}")
                if owner.id == "time":
                    violations.add(f"clock:time.{node.func.attr}")

    return tuple(sorted(violations))


@pytest.mark.parametrize(
    "operation_case",
    RECORD_OPERATION_CASES,
    ids=lambda case: case.operation,
)
@pytest.mark.parametrize("case", _fixture_cases(), ids=lambda case: str(case["id"]))
def test_adversarial_fixture_text_remains_data(
    case: dict[str, Any],
    operation_case: OperationCase,
) -> None:
    raw_text = case["input"]
    factory = SyntheticSourceFactory()

    assert_adversarial_text_remains_data(
        factory,
        raw_text,
        tuple(case["forbidden_output_fragments"]),
        operation_case,
    )


def test_scenarios_are_finite_and_unknown_scenarios_are_rejected() -> None:
    assert SCENARIOS == frozenset(
        {
            "authentication_required",
            "happy_path",
            "malformed_record",
            "missing_scope",
            "quota_exhausted",
            "rate_limited",
            "timeout",
        }
    )
    with pytest.raises(ValueError, match="unknown synthetic scenario"):
        SyntheticSourceFactory().create(
            capabilities=frozenset({Capability.HEALTH}),
            scenario="provider_selected_scenario",
        )


def test_synthetic_source_accepts_no_endpoint() -> None:
    with pytest.raises(TypeError):
        SyntheticSourceFactory().create(
            capabilities=frozenset({Capability.HEALTH}),
            endpoint="synthetic-endpoint",  # type: ignore[call-arg]
        )


def test_raw_text_payload_is_bounded() -> None:
    with pytest.raises(ValueError, match="raw_text must contain at most 4096 characters"):
        SyntheticSourceFactory().create(
            capabilities=frozenset({Capability.SEARCH_ARTISTS}),
            raw_text="x" * 4097,
        )


def test_default_text_payload_is_bounded() -> None:
    with pytest.raises(ValueError, match="provider_text must contain at most 4096 characters"):
        SyntheticSourceFactory(provider_text="x" * 4097)


def test_synthetic_source_has_no_prohibited_import_access_or_clock_call() -> None:
    source = SYNTHETIC_SOURCE_PATH.read_text(encoding="utf-8")

    assert _prohibited_accesses(source) == ()


@pytest.mark.parametrize(
    ("source", "expected"),
    PROHIBITED_ACCESS_SNIPPETS,
    ids=[expected for _, expected in PROHIBITED_ACCESS_SNIPPETS],
)
def test_prohibited_access_checker_detects_each_positive_control(
    source: str,
    expected: str,
) -> None:
    assert _prohibited_accesses(source) == (expected,)
