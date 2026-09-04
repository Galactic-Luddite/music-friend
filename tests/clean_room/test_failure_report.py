from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).parents[2]
REPORTER = ROOT / "scripts" / "clean-room-failure.py"


def _reporter() -> ModuleType:
    spec = importlib.util.spec_from_file_location("clean_room_failure", REPORTER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_pytest_summary_keeps_only_bounded_repository_test_identifiers(tmp_path: Path) -> None:
    log = tmp_path / "pytest.log"
    log.write_text(
        "\n".join(
            [
                "FAILED tests/clean_room/test_artifact.py::test_nested - private detail",
                "ERROR tests/store/test_catalog.py::test_open[param-1] - another detail",
                "FAILED /example/private.py::test_secret - must not escape",
                "FAILED tests/bad name.py::test_secret - must not escape",
                "token=should-never-appear",
            ]
        ),
        encoding="utf-8",
    )

    summary = _reporter().pytest_failures(log)

    assert summary == [
        "tests/clean_room/test_artifact.py::test_nested",
        "tests/store/test_catalog.py::test_open[param-1]",
    ]
    assert "private detail" not in json.dumps(summary)
    assert "token" not in json.dumps(summary)


def test_pytest_summary_is_deduplicated_sorted_and_limited(tmp_path: Path) -> None:
    log = tmp_path / "pytest.log"
    identifiers = [f"tests/test_{index:02d}.py::test_case" for index in range(20)]
    log.write_text(
        "\n".join(
            [
                *(f"FAILED {item} - detail" for item in reversed(identifiers)),
                f"FAILED {identifiers[0]} - duplicate",
            ]
        ),
        encoding="utf-8",
    )

    assert _reporter().pytest_failures(log) == identifiers[:10]


def test_nested_stages_accept_only_valid_failure_receipts(tmp_path: Path) -> None:
    valid = tmp_path / "case" / "success.json"
    valid.parent.mkdir()
    valid.write_text(
        json.dumps(
            {
                "certification": "development-clean-room",
                "commit": "a" * 40,
                "failed_stage": "scan-artifacts",
                "status": "fail",
                "secret": "must not escape",
            }
        ),
        encoding="utf-8",
    )
    invalid = tmp_path / "other" / "success.json"
    invalid.parent.mkdir()
    invalid.write_text(
        json.dumps(
            {
                "certification": "development-clean-room",
                "commit": "not-a-commit",
                "failed_stage": "../../private",
                "status": "fail",
            }
        ),
        encoding="utf-8",
    )

    assert _reporter().nested_failure_stages(tmp_path) == ["scan-artifacts"]


def test_missing_or_malformed_inputs_produce_empty_diagnostics(tmp_path: Path) -> None:
    malformed = tmp_path / "success.json"
    malformed.write_text("not json", encoding="utf-8")
    reporter = _reporter()

    assert reporter.pytest_failures(tmp_path / "missing.log") == []
    assert reporter.nested_failure_stages(tmp_path) == []
