"""Operational local command-line interface for Music Friend."""

from __future__ import annotations

import getpass
import json
import sys
import warnings
import webbrowser
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import TextIO

import httpx
from platformdirs import user_data_path

from music_friend import __version__
from music_friend.agent_skill import SkillInstallError
from music_friend.agent_skill import install_skill as install_agent_skill
from music_friend.configuration import LocalConfig, LocalConfigStore, RadiusUnit
from music_friend.domain import (
    InboxEntry,
    InboxState,
    RefreshMetricKind,
    RefreshRun,
    WatchlistEntry,
)
from music_friend.providers import Capability, MusicSource
from music_friend.providers.credentials import CredentialStore
from music_friend.providers.spotify.config import SpotifySettings
from music_friend.providers.spotify.oauth import AuthorizationMode, SpotifyAuthorization
from music_friend.providers.spotify.source import SpotifySource
from music_friend.providers.spotify.tokens import SpotifyTokenManager
from music_friend.providers.spotify.transport import SpotifyTransport
from music_friend.providers.store_selection import open_interactive_credential_store
from music_friend.providers.ticketmaster import (
    TICKETMASTER_CREDENTIAL_KEY,
    TicketmasterDiscoveryClient,
)
from music_friend.providers.ticketmaster.transport import TicketmasterTransport
from music_friend.store import Catalog
from music_friend.tools import MusicFriendApplication
from music_friend.tools.refresh import RefreshInvocation, refresh_once
from music_friend.tools.scheduler import (
    SchedulePlatform,
    install_schedule,
    remove_schedule,
    render_schedule,
)

ConnectorFactory = Callable[[], httpx.BaseTransport]
CredentialStoreFactory = Callable[[], CredentialStore]
BrowserOpener = Callable[[str], bool]
Prompt = Callable[[str], str]
SecretPrompt = Callable[[str], str]
RefreshRunner = Callable[[str], object]
Clock = Callable[[], datetime]
AuthorizerFactory = Callable[
    [SpotifySettings, SpotifyTokenManager, BrowserOpener], SpotifyAuthorization
]

_USAGE = (
    "Usage: music-friend setup | connect spotify | disconnect spotify | status | "
    "refresh catalog|releases|events|all | watchlist list | inbox list|show | "
    "data export|import|import-spotify|backup|restore|delete | diagnostics | "
    "schedule install|status|remove | version\n"
    "       music-friend skill install (--client codex|claude | "
    "--target SKILLS_DIRECTORY) [--replace]\n"
)


class _ConnectionFailed(RuntimeError):
    pass


def _default_connector() -> httpx.BaseTransport:
    return httpx.HTTPTransport(trust_env=False)


def _default_authorizer(
    settings: SpotifySettings, tokens: SpotifyTokenManager, browser_opener: BrowserOpener
) -> SpotifyAuthorization:
    return SpotifyAuthorization(settings=settings, tokens=tokens, browser_opener=browser_opener)


def _default_prompt(message: str) -> str:
    return input(message)


def _default_secret_prompt(message: str) -> str:
    if not sys.stdin.isatty():
        raise ValueError("protected setup requires an interactive terminal")
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        try:
            return getpass.getpass(message)
        except getpass.GetPassWarning:
            raise ValueError("protected setup requires a hidden terminal prompt") from None


@contextmanager
def _spotify_tokens(
    config: LocalConfig,
    *,
    connector_factory: ConnectorFactory,
    credential_store_factory: CredentialStoreFactory,
) -> Iterator[tuple[SpotifySettings, SpotifyTokenManager]]:
    if config.spotify_client_id is None:
        raise ValueError("Spotify is not configured")
    transport = SpotifyTransport(connector_factory())
    try:
        settings = SpotifySettings(config.spotify_client_id)
        yield (
            settings,
            SpotifyTokenManager(
                settings=settings, transport=transport, store=credential_store_factory()
            ),
        )
    finally:
        transport.close()


@contextmanager
def _spotify_source(
    config: LocalConfig,
    *,
    connector_factory: ConnectorFactory,
    credential_store_factory: CredentialStoreFactory,
    now: Clock,
) -> Iterator[MusicSource]:
    with _spotify_tokens(
        config,
        connector_factory=connector_factory,
        credential_store_factory=credential_store_factory,
    ) as (settings, tokens):
        yield SpotifySource(settings=settings, tokens=tokens, clock=now)


@contextmanager
def _ticketmaster_client(
    *,
    connector_factory: ConnectorFactory,
    credential_store_factory: CredentialStoreFactory,
    now: Clock,
) -> Iterator[TicketmasterDiscoveryClient]:
    transport = TicketmasterTransport(connector_factory())
    try:
        yield TicketmasterDiscoveryClient(
            transport,
            credential_store_factory(),
            now=now,
        )
    finally:
        transport.close()


@contextmanager
def _application_or_default(
    application: MusicFriendApplication | None,
) -> Iterator[MusicFriendApplication]:
    if application is not None:
        yield application
        return
    opened = MusicFriendApplication(
        Catalog.open(Path(user_data_path("music-friend", appauthor=False)) / "catalog.sqlite3")
    )
    try:
        yield opened
    finally:
        opened.close()


def run_cli(
    argv: Sequence[str],
    *,
    stdout: TextIO,
    stderr: TextIO,
    application: MusicFriendApplication | None = None,
    config_store: LocalConfigStore | object | None = None,
    refresh_runner: RefreshRunner | None = None,
    prompt: Prompt | None = None,
    secret_prompt: SecretPrompt | None = None,
    now: Clock | None = None,
    connector_factory: ConnectorFactory = _default_connector,
    credential_store_factory: CredentialStoreFactory = open_interactive_credential_store,
    browser_opener: BrowserOpener = webbrowser.open,
    authorizer_factory: AuthorizerFactory = _default_authorizer,
) -> int:
    """Run one local operation without accepting credentials through command arguments."""
    command, structured = _split_json(argv)
    if command == ["--help"]:
        print(_USAGE, end="", file=stdout)
        return 0
    if command[:2] == ["skill", "install"]:
        if structured:
            print(_USAGE, end="", file=stderr)
            return 2
        return _skill_install_command(command[2:], stdout, stderr)

    store = LocalConfigStore() if config_store is None else config_store
    if not callable(getattr(store, "load", None)) or not callable(getattr(store, "save", None)):
        raise ValueError("config_store must provide load and save")
    selected_prompt = _default_prompt if prompt is None else prompt
    selected_secret_prompt = _default_secret_prompt if secret_prompt is None else secret_prompt
    selected_now = _utc_now if now is None else now
    if (
        not callable(selected_prompt)
        or not callable(selected_secret_prompt)
        or not callable(selected_now)
    ):
        raise ValueError("prompts and now must be callable")
    try:
        with _application_or_default(application) as current:
            return _run_local_command(
                command,
                structured,
                current,
                store,
                refresh_runner,
                selected_prompt,
                selected_secret_prompt,
                selected_now,
                stdout,
                stderr,
                connector_factory,
                credential_store_factory,
                browser_opener,
                authorizer_factory,
            )
    except Exception:
        print("Music Friend could not complete the command.", file=stderr)
        return 1


def _split_json(argv: Sequence[str]) -> tuple[list[str], bool]:
    command = list(argv)
    if command.count("--json") == 1 and command[-1:] == ["--json"]:
        return command[:-1], True
    return command, False


def _run_local_command(
    argv: list[str],
    structured: bool,
    application: MusicFriendApplication,
    config_store: object,
    refresh_runner: RefreshRunner | None,
    prompt: Prompt,
    secret_prompt: SecretPrompt,
    now: Clock,
    stdout: TextIO,
    stderr: TextIO,
    connector_factory: ConnectorFactory,
    credential_store_factory: CredentialStoreFactory,
    browser_opener: BrowserOpener,
    authorizer_factory: AuthorizerFactory,
) -> int:
    if argv == ["setup"]:
        return _setup(config_store, prompt, secret_prompt, credential_store_factory, stdout, stderr)
    if argv == ["version"]:
        return _emit({"version": __version__}, structured, stdout)
    if argv == ["status"]:
        try:
            config = _load_config(config_store)
        except Exception:
            return _unavailable_status(application, structured, stdout)
        return _status_command(
            application,
            config,
            structured,
            stdout,
            connector_factory,
            credential_store_factory,
            now,
        )
    if argv == ["diagnostics"]:
        diagnostics = _diagnostics(application, _load_config(config_store))
        return _emit(diagnostics, structured, stdout, text=_diagnostics_text(diagnostics))
    if argv == ["connect", "spotify"]:
        return _connect(
            _load_config(config_store),
            stdout,
            stderr,
            connector_factory,
            credential_store_factory,
            browser_opener,
            authorizer_factory,
        )
    if argv == ["disconnect", "spotify"]:
        return _disconnect(
            _load_config(config_store), stdout, stderr, connector_factory, credential_store_factory
        )
    if (
        len(argv) == 2
        and argv[0] == "refresh"
        and argv[1] in {"catalog", "releases", "events", "all"}
    ):
        result = _refresh(
            argv[1],
            application,
            _load_config(config_store),
            refresh_runner,
            connector_factory,
            credential_store_factory,
            now,
        )
        return _emit_refresh(result, structured, stdout)
    if argv == ["watchlist", "list"]:
        items = [_watchlist(item) for item in application.list_watchlist(limit=100)]
        return _emit(
            {"items": items},
            structured,
            stdout,
            text=_watchlist_text(items),
        )
    if argv == ["inbox", "list"]:
        items = [_inbox(item) for item in application.list_inbox_entries(None, limit=100)]
        return _emit(
            {"items": items},
            structured,
            stdout,
            text=_inbox_text(items),
        )
    if len(argv) == 3 and argv[:2] == ["inbox", "show"]:
        detail = _inbox_detail(application, argv[2])
        if detail is not None:
            return _emit(detail, structured, stdout, text=_inbox_detail_text(detail))
        print("Music Friend record was not found.", file=stderr)
        return 2
    if len(argv) >= 2 and argv[0] == "data":
        return _data_command(argv[1:], application, prompt, structured, stdout, stderr)
    if len(argv) == 2 and argv[0] == "schedule" and argv[1] in {"install", "status", "remove"}:
        return _schedule_command(argv[1], structured, stdout, stderr)
    print(_USAGE, end="", file=stderr)
    return 2


def _skill_install_command(argv: list[str], stdout: TextIO, stderr: TextIO) -> int:
    client: str | None = None
    target: Path | None = None
    replace = False
    index = 0
    while index < len(argv):
        argument = argv[index]
        if argument == "--replace" and not replace:
            replace = True
            index += 1
            continue
        if argument in {"--client", "--target"} and index + 1 < len(argv):
            value = argv[index + 1]
            if argument == "--client" and client is None:
                client = value
            elif argument == "--target" and target is None:
                target = Path(value)
            else:
                print(_USAGE, end="", file=stderr)
                return 2
            index += 2
            continue
        print(_USAGE, end="", file=stderr)
        return 2

    if (client is None) == (target is None) or client not in {None, "codex", "claude"}:
        print(_USAGE, end="", file=stderr)
        return 2
    try:
        changed = install_agent_skill(client=client, target=target, replace=replace)
    except SkillInstallError:
        print("Music Friend could not install the skill.", file=stderr)
        return 1
    print(
        "Music Friend skill installed." if changed else "Music Friend skill is already installed.",
        file=stdout,
    )
    return 0


def _load_config(store: object) -> LocalConfig:
    result = getattr(store, "load")()
    if type(result) is not LocalConfig:
        raise ValueError("local configuration is invalid")
    return result


def _save_config(store: object, config: LocalConfig) -> None:
    getattr(store, "save")(config)


def _setup(
    config_store: object,
    prompt: Prompt,
    secret_prompt: SecretPrompt,
    credential_store_factory: CredentialStoreFactory,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    try:
        prior = _load_config(config_store)
        configured = _setup_config(prior, prompt)
        key_action = _setup_key_action(
            secret_prompt("Ticketmaster API key (blank to preserve, - to remove): ")
        )
    except Exception:
        print("Music Friend could not complete the command.", file=stderr)
        return 1

    credentials: CredentialStore | None = None
    prior_key: str | None = None
    changed_key = False
    try:
        if key_action is not _PRESERVE:
            credentials = credential_store_factory()
            prior_key = credentials.load(TICKETMASTER_CREDENTIAL_KEY)
            changed_key = True
            if key_action is None:
                credentials.delete(TICKETMASTER_CREDENTIAL_KEY)
            elif isinstance(key_action, str):
                credentials.save(TICKETMASTER_CREDENTIAL_KEY, key_action)
        _save_config(config_store, configured)
    except Exception:
        if changed_key and credentials is not None:
            try:
                if prior_key is None:
                    credentials.delete(TICKETMASTER_CREDENTIAL_KEY)
                else:
                    credentials.save(TICKETMASTER_CREDENTIAL_KEY, prior_key)
            except Exception:
                pass
        print("Music Friend could not complete the command.", file=stderr)
        return 1
    print("Music Friend setup complete.", file=stdout)
    return 0


class _Preserve:
    pass


_PRESERVE = _Preserve()


def _setup_config(prior: LocalConfig, prompt: Prompt) -> LocalConfig:
    spotify_client_id = _setup_text(
        prior.spotify_client_id,
        prompt("Spotify client ID (blank to preserve, - to clear): "),
        normalize=lambda value: value.strip(),
    )
    country, postal, radius, unit = _setup_event_area(prior, prompt)
    return LocalConfig(
        spotify_client_id=spotify_client_id,
        event_country_code=country,
        event_postal_code=postal,
        event_radius=radius,
        event_radius_unit=unit,
    )


def _setup_event_area(
    prior: LocalConfig, prompt: Prompt
) -> tuple[str | None, str | None, int | float | None, RadiusUnit | None]:
    country_text = prompt("Event country code (blank to preserve, - to clear): ")
    postal_text = prompt("Event postal code (blank to preserve, - to clear): ")
    radius_text = prompt("Event radius 1-100 (blank for default or preserve, - to clear): ")
    unit_text = prompt(
        "Event radius unit miles/kilometers (blank for default or preserve, - to clear): "
    )
    values = (country_text, postal_text, radius_text, unit_text)
    if any(value == "-" for value in values):
        return None, None, None, None
    if all(value == "" for value in values):
        return (
            prior.event_country_code,
            prior.event_postal_code,
            prior.event_radius,
            prior.event_radius_unit,
        )
    country = _setup_text(
        prior.event_country_code, country_text, normalize=lambda value: value.strip().upper()
    )
    postal = _setup_text(
        prior.event_postal_code, postal_text, normalize=lambda value: value.strip()
    )
    radius = _setup_radius(prior.event_radius, radius_text)
    unit = _setup_unit(prior.event_radius_unit, unit_text)
    if country is None or postal is None:
        raise ValueError("event area is incomplete")
    if unit is None:
        unit = "miles"
    if radius is None:
        radius = 80 if unit == "kilometers" else 50
    return country, postal, radius, unit


def _setup_text(prior: str | None, value: str, *, normalize: Callable[[str], str]) -> str | None:
    if type(value) is not str:
        raise ValueError("setup input is invalid")
    if value == "":
        return prior
    if value == "-":
        return None
    return normalize(value)


def _setup_radius(prior: int | float | None, value: str) -> int | float | None:
    if type(value) is not str:
        raise ValueError("setup input is invalid")
    if value == "":
        return prior
    if value == "-":
        return None
    try:
        numeric = float(value.strip())
    except ValueError:
        raise ValueError("event radius is invalid") from None
    return int(numeric) if numeric.is_integer() else numeric


def _setup_unit(prior: RadiusUnit | None, value: str) -> RadiusUnit | None:
    if type(value) is not str:
        raise ValueError("setup input is invalid")
    if value == "":
        return prior
    if value == "-":
        return None
    normalized = value.strip().lower()
    if normalized == "miles":
        return "miles"
    if normalized == "kilometers":
        return "kilometers"
    raise ValueError("event radius unit is invalid")


def _setup_key_action(value: str) -> str | None | _Preserve:
    if type(value) is not str:
        raise ValueError("setup input is invalid")
    if value == "":
        return _PRESERVE
    if value == "-":
        return None
    if not value.strip() or len(value) > 4096:
        raise ValueError("Ticketmaster key is invalid")
    return value


def _connect(
    config: LocalConfig,
    stdout: TextIO,
    stderr: TextIO,
    connector_factory: ConnectorFactory,
    credential_store_factory: CredentialStoreFactory,
    browser_opener: BrowserOpener,
    authorizer_factory: AuthorizerFactory,
) -> int:
    try:
        with _spotify_tokens(
            config,
            connector_factory=connector_factory,
            credential_store_factory=credential_store_factory,
        ) as (settings, tokens):
            result = authorizer_factory(settings, tokens, browser_opener).authorize(
                frozenset(Capability), mode=AuthorizationMode.DYNAMIC_LOOPBACK
            )
            if not result.authorized:
                raise _ConnectionFailed()
        print("Music Friend connected to Spotify.", file=stdout)
        return 0
    except _ConnectionFailed:
        print("Music Friend could not connect to Spotify.", file=stderr)
        return 1
    except Exception:
        print("Music Friend could not complete the command.", file=stderr)
        return 1


def _disconnect(
    config: LocalConfig,
    stdout: TextIO,
    stderr: TextIO,
    connector_factory: ConnectorFactory,
    credential_store_factory: CredentialStoreFactory,
) -> int:
    try:
        with _spotify_tokens(
            config,
            connector_factory=connector_factory,
            credential_store_factory=credential_store_factory,
        ) as (_settings, tokens):
            tokens.disconnect()
        print("Music Friend disconnected from Spotify.", file=stdout)
        return 0
    except Exception:
        print("Music Friend could not complete the command.", file=stderr)
        return 1


def _status_command(
    application: MusicFriendApplication,
    config: LocalConfig,
    structured: bool,
    stdout: TextIO,
    connector_factory: ConnectorFactory,
    credential_store_factory: CredentialStoreFactory,
    now: Clock,
) -> int:
    connection = "disconnected"
    if config.spotify_client_id is not None:
        try:
            with _spotify_tokens(
                config,
                connector_factory=connector_factory,
                credential_store_factory=credential_store_factory,
            ) as (_settings, tokens):
                connection = "connected" if tokens.status().connected else "disconnected"
        except Exception:
            return _unavailable_status(application, structured, stdout)
    payload = _catalog_status(application)
    payload.update(
        {
            "connected": connection == "connected",
            "connection": connection,
            "events": {
                "ready": _events_ready(config, connector_factory, credential_store_factory, now)
            },
        }
    )
    return _emit(payload, structured, stdout)


def _events_ready(
    config: LocalConfig,
    connector_factory: ConnectorFactory,
    credential_store_factory: CredentialStoreFactory,
    now: Clock,
) -> bool:
    if not all(
        value is not None
        for value in (
            config.event_country_code,
            config.event_postal_code,
            config.event_radius,
            config.event_radius_unit,
        )
    ):
        return False
    try:
        with _ticketmaster_client(
            connector_factory=connector_factory,
            credential_store_factory=credential_store_factory,
            now=now,
        ) as client:
            return client.is_configured()
    except Exception:
        return False


def _unavailable_status(
    application: MusicFriendApplication, structured: bool, stdout: TextIO
) -> int:
    payload = _catalog_status(application)
    payload.update(
        {
            "connected": False,
            "connection": "unavailable",
            "events": {"ready": False},
            "status": "unavailable",
        }
    )
    _emit(payload, structured, stdout)
    return 4


def _refresh(
    kind: str,
    application: MusicFriendApplication,
    config: LocalConfig,
    refresh_runner: RefreshRunner | None,
    connector_factory: ConnectorFactory,
    credential_store_factory: CredentialStoreFactory,
    now: Clock,
) -> object:
    if refresh_runner is not None:
        return refresh_runner(kind)
    checked_at = _checked_at(now)
    lock_path = Path(user_data_path("music-friend", appauthor=False)) / "refresh.lock"
    with _ticketmaster_client(
        connector_factory=connector_factory,
        credential_store_factory=credential_store_factory,
        now=now,
    ) as event_client:
        if kind == "events":
            return refresh_once(
                application,
                kind=kind,
                source_name="spotify",
                source=None,
                config=config,
                event_client=event_client,
                checked_at=checked_at,
                lock_path=lock_path,
            )
        with _spotify_source(
            config,
            connector_factory=connector_factory,
            credential_store_factory=credential_store_factory,
            now=now,
        ) as source:
            return refresh_once(
                application,
                kind=kind,
                source_name="spotify",
                source=source,
                config=config,
                event_client=event_client,
                checked_at=checked_at,
                lock_path=lock_path,
            )


def _data_command(
    argv: list[str],
    application: MusicFriendApplication,
    prompt: Prompt,
    structured: bool,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    if len(argv) in {2, 3} and argv[0] == "import-spotify" and (
        len(argv) == 2 or argv[2] == "--dry-run"
    ):
        dry_run = len(argv) == 3
        result = application.import_spotify_history(Path(argv[1]), dry_run=dry_run)
        return _emit(
            {
                "duplicates": result.duplicates,
                "first_played_at": result.first_played_at,
                "imported": result.imported,
                "last_played_at": result.last_played_at,
                "members": result.member_count,
                "non_music": result.non_music,
                "status": "validated" if dry_run else "imported",
            },
            structured,
            stdout,
            text="Spotify history validated." if dry_run else "Spotify history imported.",
        )
    if len(argv) == 2 and argv[0] in {"export", "backup"}:
        return _emit(
            {"records": application.export_data(Path(argv[1])).record_count},
            structured,
            stdout,
            text="Data export complete.",
        )
    if len(argv) == 2 and argv[0] == "import":
        return _emit(
            {"records": application.import_data(Path(argv[1])).record_count},
            structured,
            stdout,
            text="Data import complete.",
        )
    if len(argv) == 2 and argv[0] == "restore":
        if prompt("Type RESTORE to continue: ") != "RESTORE":
            print("Confirmation was not accepted.", file=stderr)
            return 2
        return _emit(
            {"records": application.import_data(Path(argv[1])).record_count},
            structured,
            stdout,
            text="Data restore complete.",
        )
    if argv == ["delete"]:
        if prompt("Type DELETE to continue: ") != "DELETE":
            print("Confirmation was not accepted.", file=stderr)
            return 2
        application.delete_data()
        return _emit({"status": "deleted"}, structured, stdout, text="Local data deleted.")
    print(_USAGE, end="", file=stderr)
    return 2


def _schedule_command(action: str, structured: bool, stdout: TextIO, stderr: TextIO) -> int:
    platform = _schedule_platform()
    root = Path.home()
    command = ("music-friend", "refresh", "all")
    try:
        if action == "install":
            install_schedule(platform, user_root=root, command=command, interval_minutes=360)
            return _emit({"status": "installed"}, structured, stdout, text="Schedule: installed.")
        if action == "remove":
            remove_schedule(platform, user_root=root)
            return _emit({"status": "removed"}, structured, stdout, text="Schedule: removed.")
        path = root / render_schedule(platform, command=command, interval_minutes=360).relative_path
        installed = path.is_file()
        return _emit(
            {"installed": installed},
            structured,
            stdout,
            text="Schedule: installed." if installed else "Schedule: not installed.",
        )
    except Exception:
        print("Music Friend could not complete the command.", file=stderr)
        return 1


def _schedule_platform() -> SchedulePlatform:
    if sys.platform == "darwin":
        return SchedulePlatform.MACOS
    if sys.platform == "win32":
        return SchedulePlatform.WINDOWS
    return SchedulePlatform.LINUX


def _emit_refresh(value: object, structured: bool, stdout: TextIO) -> int:
    payload = _refresh_payload(value)
    _emit(payload, structured, stdout)
    return 3 if payload["status"] == "partial" else 0 if payload["status"] == "succeeded" else 1


def _refresh_payload(value: object) -> dict[str, object]:
    if (
        isinstance(value, Mapping)
        and type(value.get("status")) is str
        and set(value) <= {"kind", "status"}
    ):
        return dict(value)
    if isinstance(value, RefreshInvocation):
        if value.already_running:
            return {"status": "partial"}
        if value.run is not None:
            return _refresh_run(value.run)
    raise ValueError("refresh result is invalid")


def _catalog_status(application: MusicFriendApplication) -> dict[str, object]:
    latest = application.list_refresh_runs(limit=1)
    return {
        "status": "ready",
        "inbox": {"has_unread": bool(application.list_inbox_entries(InboxState.UNREAD, limit=1))},
        "latest_refresh": None if not latest else _refresh_run(latest[0]),
    }


def _diagnostics(application: MusicFriendApplication, config: LocalConfig) -> dict[str, object]:
    return {
        "status": "ready",
        "spotify_configured": config.spotify_client_id is not None,
        "event_area_configured": all(
            value is not None
            for value in (
                config.event_country_code,
                config.event_postal_code,
                config.event_radius,
                config.event_radius_unit,
            )
        ),
        "latest_refresh": _catalog_status(application)["latest_refresh"],
        "source_limits": {"spotify": _source_limit_diagnostics(application, "spotify")},
    }


def _source_limit_diagnostics(
    application: MusicFriendApplication, source: str
) -> dict[str, object]:
    observation = application.get_source_limit(source)
    requests = 0
    pauses = 0
    for run in application.list_refresh_runs(limit=500):
        if run.source != source:
            continue
        metrics = {metric.kind: metric.count for metric in run.summary.metrics}
        requests = metrics.get(RefreshMetricKind.SOURCE_REQUESTS, 0)
        pauses = metrics.get(RefreshMetricKind.LIMIT_PAUSES, 0)
        break
    return {
        "state": "available" if observation is None else observation.state.value,
        "observed_at": None if observation is None else observation.observed_at.isoformat(),
        "retry_at": (
            None
            if observation is None or observation.retry_at is None
            else observation.retry_at.isoformat()
        ),
        "retry_is_exact": False if observation is None else observation.retry_is_exact,
        "consecutive_limits": 0 if observation is None else observation.consecutive_limits,
        "last_refresh_requests": requests,
        "last_refresh_pauses": pauses,
    }


def _watchlist(value: WatchlistEntry) -> dict[str, object]:
    artist = value.artist
    return {
        "artist": {"local_id": artist.local_id, "display_name": artist.display_name},
        "inclusion_reason": value.inclusion_reason.value,
        "affinity_points": value.affinity.total_points,
    }


def _inbox(value: InboxEntry) -> dict[str, object]:
    return {
        "local_id": value.local_id,
        "state": value.state.value,
        "created_at": value.created_at.isoformat(),
        "updated_at": value.updated_at.isoformat(),
    }


def _inbox_detail(application: MusicFriendApplication, local_id: str) -> dict[str, object] | None:
    entry = application.get_inbox_entry(local_id)
    if entry is None:
        return None
    signal = application.get_signal(entry.signal_local_id)
    if signal is None:
        return None
    return {
        "entry": _inbox(entry),
        "kind": signal.kind.value,
        "record_id": signal.record_local_id,
        "reasons": [
            {"kind": reason.kind.value, "detail": reason.detail}
            for reason in signal.explanation.reasons
        ],
    }


def _refresh_run(value: RefreshRun) -> dict[str, object]:
    return {
        "kind": value.kind.value,
        "status": value.status.value,
        "started_at": value.started_at.isoformat(),
        "finished_at": None if value.finished_at is None else value.finished_at.isoformat(),
        "metrics": [
            {"kind": item.kind.value, "count": item.count} for item in value.summary.metrics
        ],
    }


def _emit(
    payload: dict[str, object], structured: bool, stdout: TextIO, *, text: str | None = None
) -> int:
    print(
        json.dumps(payload, separators=(",", ":"), sort_keys=True)
        if structured
        else _text(payload)
        if text is None
        else text,
        file=stdout,
    )
    return 0


def _text(payload: dict[str, object]) -> str:
    if type(payload.get("version")) is str:
        return f"Music Friend version {payload['version']}"
    if type(payload.get("kind")) is str and type(payload.get("status")) is str:
        return f"Refresh {payload['kind']}: {payload['status']}."
    if type(payload.get("status")) is str:
        connection = payload.get("connection")
        suffix = f" (Spotify: {connection})" if type(connection) is str else ""
        events = payload.get("events")
        if isinstance(events, dict) and type(events.get("ready")) is bool:
            suffix = suffix.removesuffix(")") + (
                f"; Events: {'ready' if events['ready'] else 'not ready'})"
            )
        return f"Music Friend: {payload['status']}{suffix}"
    if type(payload.get("records")) is int:
        return f"Records processed: {payload['records']}."
    if type(payload.get("installed")) is bool:
        return "Schedule: installed." if payload["installed"] else "Schedule: not installed."
    return "Music Friend command completed."


def _watchlist_text(items: list[dict[str, object]]) -> str:
    if not items:
        return "Watchlist: no monitored artists."
    names = [
        artist.get("display_name")
        for item in items
        if isinstance((artist := item.get("artist")), dict)
    ]
    return "Watchlist: " + ", ".join(name for name in names if type(name) is str) + "."


def _inbox_text(items: list[dict[str, object]]) -> str:
    if not items:
        return "Inbox: no items."
    unread = sum(item.get("state") == "unread" for item in items)
    return f"Inbox: {len(items)} item(s), {unread} unread."


def _inbox_detail_text(detail: dict[str, object]) -> str:
    entry = detail["entry"]
    if not isinstance(entry, dict):
        return "Inbox item details are unavailable."
    local_id, state = entry.get("local_id"), entry.get("state")
    if type(local_id) is not str or type(state) is not str:
        return "Inbox item details are unavailable."
    return f"Inbox item {local_id}: {state}."


def _diagnostics_text(diagnostics: dict[str, object]) -> str:
    latest = diagnostics.get("latest_refresh")
    latest_text = "none"
    if isinstance(latest, dict) and type(latest.get("kind")) is str:
        latest_text = str(latest["kind"])
    return "\n".join(
        (
            f"Music Friend diagnostics: {diagnostics['status']}",
            "Spotify configuration: "
            + ("available" if diagnostics["spotify_configured"] is True else "not configured"),
            "Event area: "
            + ("configured" if diagnostics["event_area_configured"] is True else "not configured"),
            f"Latest refresh: {latest_text}",
        )
    )


def _checked_at(clock: Clock) -> datetime:
    value = clock()
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("clock is invalid")
    return value.astimezone(timezone.utc)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def main() -> None:
    result = run_cli(sys.argv[1:], stdout=sys.stdout, stderr=sys.stderr)
    if result != 0:
        raise SystemExit(result)


if __name__ == "__main__":
    main()
