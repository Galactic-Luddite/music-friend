"""Boundary tests for CLI normalization and safe text rendering."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from music_friend.configuration import LocalConfig
from music_friend.runtimes import cli

NOW = datetime(2026, 9, 2, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    ("prior", "value", "expected"),
    (("prior", "", "prior"), ("prior", "-", None), (None, " value ", "VALUE")),
)
def test_setup_text_preserves_clears_or_normalizes(
    prior: str | None, value: str, expected: str | None
) -> None:
    assert cli._setup_text(prior, value, normalize=lambda item: item.strip().upper()) == expected


def test_setup_text_rejects_non_text_input() -> None:
    with pytest.raises(ValueError):
        cli._setup_text(None, 1, normalize=str)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("prior", "value", "expected"),
    ((10, "", 10), (10, "-", None), (None, "25", 25), (None, "2.5", 2.5)),
)
def test_setup_radius_normalizes_valid_values(
    prior: int | float | None, value: str, expected: int | float | None
) -> None:
    assert cli._setup_radius(prior, value) == expected


@pytest.mark.parametrize("value", (None, "invalid"))
def test_setup_radius_rejects_invalid_input(value: object) -> None:
    with pytest.raises(ValueError):
        cli._setup_radius(None, value)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("prior", "value", "expected"),
    (
        ("miles", "", "miles"),
        ("miles", "-", None),
        (None, " MILES ", "miles"),
        (None, "KILOMETERS", "kilometers"),
    ),
)
def test_setup_unit_normalizes_closed_units(prior: object, value: str, expected: object) -> None:
    assert cli._setup_unit(prior, value) == expected  # type: ignore[arg-type]


@pytest.mark.parametrize("value", (None, "yards"))
def test_setup_unit_rejects_invalid_input(value: object) -> None:
    with pytest.raises(ValueError):
        cli._setup_unit(None, value)  # type: ignore[arg-type]


def test_setup_key_action_preserves_clears_and_validates() -> None:
    assert cli._setup_key_action("") is cli._PRESERVE
    assert cli._setup_key_action("-") is None
    assert cli._setup_key_action("key") == "key"
    for value in (None, " ", "x" * 4097):
        with pytest.raises(ValueError):
            cli._setup_key_action(value)  # type: ignore[arg-type]


@pytest.mark.parametrize(("value", "expected"), (("", True), ("YES", True), ("n", False)))
def test_daily_schedule_choice_is_explicit_and_defaults_to_enabled(
    value: str, expected: bool
) -> None:
    assert cli._daily_schedule_choice(value) is expected


def test_daily_schedule_choice_rejects_ambiguous_input() -> None:
    with pytest.raises(ValueError, match="schedule choice"):
        cli._daily_schedule_choice("maybe")


def test_config_loader_requires_exact_local_config_type() -> None:
    assert cli._load_config(SimpleNamespace(load=lambda: LocalConfig())) == LocalConfig()
    with pytest.raises(ValueError):
        cli._load_config(SimpleNamespace(load=lambda: object()))


def test_event_area_supplies_unit_specific_defaults_and_rejects_incomplete_area() -> None:
    prior = LocalConfig()
    values = iter(("US", "94105", "", "kilometers"))
    assert cli._setup_event_area(prior, lambda _message: next(values)) == (
        "US",
        "94105",
        80,
        "kilometers",
    )
    incomplete = iter(("US", "", "", "miles"))
    with pytest.raises(ValueError):
        cli._setup_event_area(prior, lambda _message: next(incomplete))


def test_text_renderers_handle_empty_and_malformed_results() -> None:
    assert cli._watchlist_text([]) == "Watchlist: no monitored artists."
    assert cli._inbox_detail_text({"entry": None}) == "Inbox item details are unavailable."
    assert cli._inbox_detail_text({"entry": {"local_id": 1, "state": None}}) == (
        "Inbox item details are unavailable."
    )
    assert cli._text({"installed": True}) == "Schedule: installed."
    assert cli._text({"installed": False}) == "Schedule: not installed."
    assert cli._text({}) == "Music Friend command completed."


def test_utc_clock_is_aware() -> None:
    value = cli._utc_now()
    assert isinstance(value, datetime)
    assert value.tzinfo is timezone.utc


def test_watchlist_and_inbox_serializers_return_stable_local_shapes() -> None:
    watchlist = SimpleNamespace(
        artist=SimpleNamespace(local_id="artist", display_name="Artist"),
        inclusion_reason=SimpleNamespace(value="automatic"),
        affinity=SimpleNamespace(total_points=12),
    )
    entry = SimpleNamespace(
        local_id="inbox",
        signal_local_id="signal",
        state=SimpleNamespace(value="unread"),
        created_at=NOW,
        updated_at=NOW,
    )
    assert cli._watchlist(watchlist)["affinity_points"] == 12
    assert cli._inbox(entry) == {
        "local_id": "inbox",
        "state": "unread",
        "created_at": NOW.isoformat(),
        "updated_at": NOW.isoformat(),
    }


def test_inbox_detail_handles_missing_entry_signal_and_complete_result() -> None:
    entry = SimpleNamespace(
        local_id="inbox",
        signal_local_id="signal",
        state=SimpleNamespace(value="unread"),
        created_at=NOW,
        updated_at=NOW,
    )
    signal = SimpleNamespace(
        kind=SimpleNamespace(value="release"),
        record_local_id="release",
        explanation=SimpleNamespace(
            reasons=(SimpleNamespace(kind=SimpleNamespace(value="new_release"), detail=None),)
        ),
    )
    assert cli._inbox_detail(SimpleNamespace(get_inbox_entry=lambda _id: None), "id") is None
    assert (
        cli._inbox_detail(
            SimpleNamespace(get_inbox_entry=lambda _id: entry, get_signal=lambda _id: None), "id"
        )
        is None
    )
    detail = cli._inbox_detail(
        SimpleNamespace(get_inbox_entry=lambda _id: entry, get_signal=lambda _id: signal), "id"
    )
    assert detail is not None
    assert detail["record_id"] == "release"


def test_main_exits_only_for_nonzero_cli_result(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "run_cli", lambda *_args, **_kwargs: 0)
    cli.main()
    monkeypatch.setattr(cli, "run_cli", lambda *_args, **_kwargs: 2)
    with pytest.raises(SystemExit) as raised:
        cli.main()
    assert raised.value.code == 2


def test_default_secret_prompt_requires_a_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)
    with pytest.raises(ValueError):
        cli._default_secret_prompt("Secret: ")
