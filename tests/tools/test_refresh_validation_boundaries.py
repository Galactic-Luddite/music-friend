"""Focused behavior tests for refresh control and lock boundaries."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from music_friend.configuration import LocalConfig
from music_friend.domain.models import RefreshKind, RefreshStatus
from music_friend.tools import refresh

NOW = datetime(2026, 9, 2, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    ("kind", "expected"),
    (
        (RefreshKind.CATALOG, ("catalog",)),
        (RefreshKind.RELEASES, ("releases",)),
        (RefreshKind.EVENTS, ("events",)),
        (RefreshKind.ALL, ("catalog", "releases", "events")),
    ),
)
def test_refresh_components_are_closed_and_ordered(
    kind: RefreshKind, expected: tuple[str, ...]
) -> None:
    assert refresh._refresh_kind(kind) is kind
    assert refresh._refresh_kind(kind.value) is kind
    assert refresh._components(kind) == expected


@pytest.mark.parametrize("value", (None, "unknown", 1))
def test_refresh_kind_rejects_unknown_values(value: object) -> None:
    with pytest.raises(ValueError):
        refresh._refresh_kind(value)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("partial", "successes", "failures", "expected"),
    (
        (True, 1, 0, RefreshStatus.PARTIAL),
        (False, 1, 0, RefreshStatus.SUCCEEDED),
        (False, 0, 1, RefreshStatus.FAILED),
        (False, 1, 1, RefreshStatus.PARTIAL),
    ),
)
def test_refresh_status_reflects_partial_success_and_failure(
    partial: bool, successes: int, failures: int, expected: RefreshStatus
) -> None:
    counts = refresh._RefreshCounts(partial=partial, successes=successes, failures=failures)
    assert refresh._status(counts) is expected


def test_refresh_summary_keeps_zero_operational_limit_metrics() -> None:
    summary = refresh._summary(refresh._RefreshCounts(records_seen=2))
    values = {metric.kind.value: metric.count for metric in summary.metrics}
    assert values == {"limit_pauses": 0, "records_seen": 2, "source_requests": 0}


def test_catalog_and_event_runner_contain_source_failures() -> None:
    class Application:
        def synchronize_catalog(self, *_args: object) -> object:
            raise RuntimeError

        def discover_ticketmaster_events(self, **_kwargs: object) -> object:
            raise RuntimeError

    counts = refresh._RefreshCounts()
    source = SimpleNamespace(stopped=False, limit_observation=None)
    refresh._run_catalog(Application(), "source", source, counts)  # type: ignore[arg-type]
    refresh._run_events(Application(), LocalConfig(), None, NOW, counts)  # type: ignore[arg-type]
    refresh._run_events(Application(), LocalConfig(), object(), NOW, counts)  # type: ignore[arg-type]
    assert counts.failures == 2
    assert counts.successes == 1
    assert counts.records_skipped == 1


@pytest.mark.parametrize(
    "content",
    (
        "not-json",
        "[]",
        '{"created_at":"now","token":"token"}',
        '{"created_at":1,"token":""}',
        '{"created_at":1,"token":"token","extra":1}',
    ),
)
def test_refresh_lock_reader_rejects_malformed_documents(tmp_path: Path, content: str) -> None:
    path = tmp_path / "refresh.lock"
    path.write_text(content, encoding="utf-8")
    assert refresh._read_lock(path) is None


def test_refresh_lock_rejects_invalid_clock_and_contended_or_substituted_lock(
    tmp_path: Path,
) -> None:
    path = tmp_path / "refresh.lock"
    with pytest.raises(ValueError):
        refresh._acquire_lock(path, lock_clock=lambda: "now")  # type: ignore[return-value]
    lease = refresh._acquire_lock(path, lock_clock=lambda: 1.0)
    assert lease is not None
    assert refresh._acquire_lock(path, lock_clock=lambda: 2.0) is None
    replacement = tmp_path / "replacement"
    replacement.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    path.unlink()
    replacement.rename(path)
    refresh._release_lock(lease)
    assert path.exists()


def test_refresh_lock_release_ignores_wrong_type_missing_and_changed_token(tmp_path: Path) -> None:
    refresh._release_lock(object())  # type: ignore[arg-type]
    path = tmp_path / "refresh.lock"
    lease = refresh._acquire_lock(path, lock_clock=lambda: 1.0)
    assert lease is not None
    path.write_text('{"created_at":1,"token":"changed"}', encoding="utf-8")
    refresh._release_lock(lease)
    assert path.exists()
