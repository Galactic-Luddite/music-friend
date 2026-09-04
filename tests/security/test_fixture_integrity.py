from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

FIXTURE_PATH = Path(__file__).parent / "injection-fixtures" / "source_text.json"
REQUIRED_CLASSES = {
    "c0_c1_controls",
    "ansi_terminal",
    "zero_width",
    "bidi_controls",
    "homoglyphs",
    "nested_delimiters",
    "code_fence_breakout",
    "fake_system_message",
    "fake_tool_result",
    "instruction_like",
    "oversized_field",
}
ID_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
PATH_SEPARATOR_PATTERN = "(?:/|" + re.escape(chr(92)) + ")"
PROHIBITED_TEXT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "credential",
        re.compile(
            r"(?i)\b(?:api[-_ ]?key|access[-_ ]?key|client[-_ ]?secret|password|passwd|"
            r"secret|token|authorization)\b\s*[:=]\s*\S+"
        ),
    ),
    (
        "token",
        re.compile(
            r"(?i)\b(?:sk|pk|rk)[-_][A-Za-z0-9_-]{16,}\b|"
            r"\bgh[pousr]_[A-Za-z0-9]{20,}\b|"
            r"\bxox[baprs]-[A-Za-z0-9-]{16,}\b|"
            r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"
        ),
    ),
    ("access_key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    (
        "private_key",
        re.compile(
            r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
            re.IGNORECASE,
        ),
    ),
    (
        "url",
        re.compile(r"(?i)\b(?:[a-z][a-z0-9+.-]{1,31}://|(?:mailto|file|ftp|ssh|sftp):)"),
    ),
    (
        "email",
        re.compile(r"(?i)(?<![\w.+-])[\w.+-]+@[a-z0-9-]+(?:\.[a-z0-9-]+)+(?![\w.-])"),
    ),
    (
        "hostname",
        re.compile(
            r"(?i)(?<![@\w.-])(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
            r"[a-z]{2,63}(?![\w.-])"
        ),
    ),
    (
        "path",
        re.compile(
            r"(?i)(?:^|[\s\"'=])(?:~" + PATH_SEPARATOR_PATTERN + "|"
            r"/(?:Users|home|workspace|workspaces|repos|repositories|projects|checkouts|src)/\S+|"
            r"[A-Z]:"
            + PATH_SEPARATOR_PATTERN
            + r"(?:Users|workspace|workspaces|repos|repositories|projects|checkouts|src)"
            + PATH_SEPARATOR_PATTERN
            + r"\S+)"
        ),
    ),
    (
        "phone",
        re.compile(r"(?<!\d)(?:\+?\d{1,3}[ .-]?)?(?:\(?\d{3}\)?[ .-])\d{3}[ .-]\d{4}(?!\d)"),
    ),
    ("personal_id", re.compile(r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)")),
    (
        "network_address",
        re.compile(
            r"(?<!\d)(?:25[0-5]|2[0-4]\d|1?\d?\d)(?:\."
            r"(?:25[0-5]|2[0-4]\d|1?\d?\d)){3}(?!\d)"
        ),
    ),
    (
        "private_context",
        re.compile(
            r"(?i)\b(?:private|confidential|internal(?:[-_ ]only)?|do[-_ ]not[-_ ]share|"
            r"proprietary|sensitive[-_ ]data|personal[-_ ]data)\b"
        ),
    ),
)
EMOJI_AND_VARIATION_RANGES = (
    (0x20E3, 0x20E3),
    (0x2300, 0x23FF),
    (0x2600, 0x27BF),
    (0x2B00, 0x2BFF),
    (0xFE00, 0xFE0F),
    (0x1F000, 0x1FAFF),
    (0xE0100, 0xE01EF),
)


def _load_fixture() -> dict[str, Any]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def _iter_strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for nested_value in value.values():
            yield from _iter_strings(nested_value)
    elif isinstance(value, list):
        for nested_value in value:
            yield from _iter_strings(nested_value)


def _contains_emoji_or_variation_selector(value: str) -> bool:
    return any(
        lower <= ord(character) <= upper
        for character in value
        for lower, upper in EMOJI_AND_VARIATION_RANGES
    )


def _assert_cases_are_synthetic_and_public(cases: list[dict[str, Any]]) -> None:
    for value in _iter_strings(cases):
        if _contains_emoji_or_variation_selector(value):
            raise AssertionError("fixture safety violation: emoji")
        for rule, pattern in PROHIBITED_TEXT_PATTERNS:
            if pattern.search(value):
                raise AssertionError(f"fixture safety violation: {rule}")


def test_fixture_schema_ids_and_required_classes_are_complete() -> None:
    fixture = _load_fixture()
    cases = fixture["cases"]
    ids = [case["id"] for case in cases]
    actual_classes = {case["class"] for case in cases}
    class_counts = {
        actual_class: sum(case["class"] == actual_class for case in cases)
        for actual_class in actual_classes
    }

    assert fixture["schema_version"] == 1
    assert len(ids) == len(set(ids))
    assert all(ID_PATTERN.fullmatch(case_id) for case_id in ids)
    assert actual_classes == REQUIRED_CLASSES
    assert all(count >= 2 for count in class_counts.values())
    assert all(isinstance(case["input"], str) for case in cases)
    assert all(case["forbidden_output_fragments"] for case in cases)


def test_fixture_contains_only_synthetic_public_context() -> None:
    _assert_cases_are_synthetic_and_public(_load_fixture()["cases"])


@pytest.mark.parametrize(
    ("value", "expected_rule"),
    [
        ("pass" + "word=" + "synthetic-value", "credential"),
        ("sk" + "-" + "A" * 24, "token"),
        ("AK" + "IA" + "A" * 16, "access_key"),
        ("-----BEGIN " + "PRIVATE KEY-----", "private_key"),
        ("custom" + "://" + "node" + ".invalid", "url"),
        ("node" + ".invalid", "hostname"),
        ("/" + "home" + "/" + "sample" + "/notes", "path"),
        ("/" + "Users" + "/" + "sample" + "/notes", "path"),
        ("C:" + chr(92) + "Users" + chr(92) + "sample" + chr(92) + "notes", "path"),
        ("/" + "workspace" + "/" + "sample" + "/checkout", "path"),
        (chr(0x1F680), "emoji"),
        ("sample" + chr(0xFE0F), "emoji"),
        ("sample" + "@" + "mail" + ".invalid", "email"),
        ("555" + "-" + "010" + "-" + "2048", "phone"),
        ("000" + "-" + "12" + "-" + "3456", "personal_id"),
        (".".join(["192", "0", "2", "1"]), "network_address"),
        ("confi" + "dential sample", "private_context"),
    ],
    ids=[
        "credential-assignment",
        "token-shape",
        "access-key-shape",
        "private-key-shape",
        "non-http-url-scheme",
        "hostname-shape",
        "posix-home-path",
        "macos-home-path",
        "windows-home-path",
        "checkout-path",
        "emoji",
        "variation-selector",
        "email-shape",
        "phone-shape",
        "government-id-shape",
        "network-address-shape",
        "private-context-marker",
    ],
)
def test_integrity_validator_rejects_prohibited_shapes_without_echoing(
    value: str, expected_rule: str
) -> None:
    cases = [
        {
            "id": "synthetic-positive-control",
            "class": "synthetic",
            "input": value,
            "forbidden_output_fragments": ["sample"],
        }
    ]

    with pytest.raises(AssertionError) as error:
        _assert_cases_are_synthetic_and_public(cases)

    assert str(error.value) == f"fixture safety violation: {expected_rule}"
    assert value not in str(error.value)


def test_fixture_file_is_bounded() -> None:
    assert FIXTURE_PATH.stat().st_size <= 256 * 1024
