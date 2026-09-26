"""Operational local command-line interface for Music Friend."""

from __future__ import annotations

import getpass
import json
import os
import stat
import sys
import warnings
import webbrowser
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import TextIO

import httpx
from platformdirs import user_data_path

from music_friend import __version__
from music_friend.agent_skill import SkillInstallError
from music_friend.agent_skill import install_skill as install_agent_skill
from music_friend.configuration import (
    DEFAULT_RELEASE_SOURCES,
    LocalConfig,
    LocalConfigStore,
    RadiusUnit,
)
from music_friend.domain import (
    DAILY_REFRESH_MINUTES,
    InboxEntry,
    InboxState,
    RefreshMetricKind,
    RefreshRun,
    WatchlistEntry,
)
from music_friend.providers import Capability, MusicSource
from music_friend.providers.credentials import CredentialStore
from music_friend.providers.keyring_store import KeyringCredentialStore
from music_friend.providers.musicbrainz.source import MusicBrainzSource
from music_friend.providers.musicbrainz.transport import MusicBrainzTransport
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
    ScheduleStatus,
    install_schedule,
    remove_schedule,
    schedule_status,
)

ConnectorFactory = Callable[[], httpx.BaseTransport]
CredentialStoreFactory = Callable[[], CredentialStore]
BrowserOpener = Callable[[str], bool]
Prompt = Callable[[str], str]
SecretPrompt = Callable[[str], str]
RefreshRunner = Callable[[str], object]
Clock = Callable[[], datetime]
NativeStoreProbe = Callable[[], bool]
AuthorizerFactory = Callable[
    [SpotifySettings, SpotifyTokenManager, BrowserOpener], SpotifyAuthorization
]

_USAGE = (
    "Usage: music-friend doctor | setup [--release-sources spotify,musicbrainz,deezer] | connect spotify | disconnect spotify | status | "
    "refresh catalog|releases|events|all [--force] | watchlist list | inbox list|show | "
    "data export|import|import-spotify|backup|restore|delete | diagnostics | "
    "schedule install|status|remove | version\n"
    "       music-friend skill install (--client codex|claude | "
    "--target SKILLS_DIRECTORY) [--replace]\n"
)
#: Cadence of the scheduled full refresh. Shared with release_discovery.FRESHNESS_TTL via
#: music_friend.domain.DAILY_REFRESH_MINUTES so the freshness TTL always stays well below
#: this interval.
_DAILY_REFRESH_MINUTES = DAILY_REFRESH_MINUTES


class _ConnectionFailed(RuntimeError):
    pass


class _ProviderNotConfigured(RuntimeError):
    """Raised when a provider-backed command needs configuration that is absent."""


_PROVIDER_NOT_CONFIGURED_MESSAGE = (
    "Spotify is not configured. Run `music-friend doctor` for setup guidance."
)


def _default_connector() -> httpx.BaseTransport:
    return httpx.HTTPTransport(trust_env=False)


def _default_authorizer(
    settings: SpotifySettings, tokens: SpotifyTokenManager, browser_opener: BrowserOpener
) -> SpotifyAuthorization:
    return SpotifyAuthorization(settings=settings, tokens=tokens, browser_opener=browser_opener)


def _native_store_available() -> bool:
    """Report whether an approved native credential store opens, without reading any value."""
    try:
        KeyringCredentialStore()
    except Exception:
        return False
    return True


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
        raise _ProviderNotConfigured("Spotify is not configured")
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
def _musicbrainz_source(
    *, connector_factory: ConnectorFactory, now: Clock
) -> Iterator[MusicSource]:
    """MusicBrainz is keyless: no credential store needed, but the same injectable
    connector as every other provider so tests never need a live network call."""
    transport = MusicBrainzTransport(
        user_agent=f"music-friend/{__version__} (https://github.com/Galactic-Luddite/music-friend)",
        connector=connector_factory(),
    )
    source = MusicBrainzSource(transport=transport, clock=now)
    try:
        yield source
    finally:
        source.close()


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
    native_store_probe: NativeStoreProbe | None = None,
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
    if (
        len(command) == 2
        and command[0] == "schedule"
        and command[1]
        in {
            "install",
            "status",
            "remove",
        }
    ):
        return _schedule_command(command[1], structured, stdout, stderr)

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
                _native_store_available if native_store_probe is None else native_store_probe,
            )
    except _ProviderNotConfigured:
        print(_PROVIDER_NOT_CONFIGURED_MESSAGE, file=stderr)
        return 1
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
    native_store_probe: NativeStoreProbe,
) -> int:
    if argv == ["doctor"]:
        return _doctor(
            application,
            config_store,
            structured,
            stdout,
            connector_factory,
            credential_store_factory,
            native_store_probe,
            now,
        )
    if len(argv) >= 1 and argv[0] == "setup":
        return _setup_command(
            argv[1:],
            config_store,
            prompt,
            secret_prompt,
            credential_store_factory,
            structured,
            stdout,
            stderr,
        )
    if argv == ["version"]:
        return _emit({"version": __version__}, structured, stdout)
    if argv == ["status"]:
        try:
            config = _load_config(config_store)
        except Exception:
            return _unavailable_status(application, structured, stdout, native_store_probe)
        return _status_command(
            application,
            config,
            structured,
            stdout,
            connector_factory,
            credential_store_factory,
            native_store_probe,
            now,
        )
    if argv == ["diagnostics"]:
        diagnostics = _diagnostics(application, _load_config(config_store), now)
        return _emit(diagnostics, structured, stdout, text=_diagnostics_text(diagnostics))
    if len(argv) >= 2 and argv[:2] == ["connect", "spotify"]:
        return _connect_command(
            argv[2:],
            _load_config(config_store),
            structured,
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
        len(argv) >= 2
        and argv[0] == "refresh"
        and argv[1] in {"catalog", "releases", "events", "all"}
        and (len(argv) == 2 or (len(argv) == 3 and argv[2] == "--force"))
    ):
        force = len(argv) == 3
        result = _refresh(
            argv[1],
            application,
            _load_config(config_store),
            refresh_runner,
            connector_factory,
            credential_store_factory,
            now,
            force=force,
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
    try:
        release_sources_input = prompt(
            "Choose release sources, comma-separated (default: musicbrainz): "
            "[spotify|musicbrainz|deezer] "
        )
    except StopIteration:
        release_sources_input = ""
    release_sources = _setup_release_sources(prior.release_sources, release_sources_input)
    return LocalConfig(
        spotify_client_id=spotify_client_id,
        event_country_code=country,
        event_postal_code=postal,
        event_radius=radius,
        event_radius_unit=unit,
        release_sources=release_sources,
    )


_ALLOWED_RELEASE_SOURCE_TOKENS = frozenset({"spotify", "musicbrainz", "deezer"})


def _setup_release_sources(prior: tuple[str, ...], value: str) -> tuple[str, ...]:
    """Parse a comma-separated release-sources token list from a prompt or flag.

    Blank input preserves ``prior``. Each token must be one of spotify,
    musicbrainz, or deezer, with no duplicates.
    """
    if type(value) is not str:
        raise ValueError("release_sources is invalid")
    normalized = value.strip().lower()
    if not normalized:
        return prior
    tokens = tuple(token.strip() for token in normalized.split(","))
    if not tokens or any(not token for token in tokens):
        raise ValueError("release_sources is invalid")
    if len(set(tokens)) != len(tokens):
        raise ValueError("release_sources is invalid")
    if not all(token in _ALLOWED_RELEASE_SOURCE_TOKENS for token in tokens):
        raise ValueError("release_sources is invalid")
    return tokens


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
    except _ProviderNotConfigured:
        print(_PROVIDER_NOT_CONFIGURED_MESSAGE, file=stderr)
        return 1
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
    native_store_probe: NativeStoreProbe,
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
            return _unavailable_status(application, structured, stdout, native_store_probe)
    payload = _catalog_status(application)
    payload.update(
        {
            "connected": connection == "connected",
            "connection": connection,
            "events": {
                "ready": _events_ready(config, connector_factory, credential_store_factory, now)
            },
            "mcp_ready": _probe(native_store_probe),
            "source_limits": {
                "spotify": _source_limit_diagnostics(application, "spotify", _checked_at(now))
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
    application: MusicFriendApplication,
    structured: bool,
    stdout: TextIO,
    native_store_probe: NativeStoreProbe,
) -> int:
    payload = _catalog_status(application)
    payload.update(
        {
            "connected": False,
            "connection": "unavailable",
            "events": {"ready": False},
            "mcp_ready": _probe(native_store_probe),
            "status": "unavailable",
        }
    )
    _emit(payload, structured, stdout)
    return 4


def _probe(native_store_probe: NativeStoreProbe) -> bool:
    try:
        return native_store_probe() is True
    except Exception:
        return False


_DOCTOR_REMEDIES = {
    "python": "Install Python 3.10 or newer.",
    "credential_store": (
        "Install and unlock a native credential store (macOS Keychain, Windows Credential "
        "Manager, or Secret Service/KWallet on Linux). The MCP server and schedules require it; "
        "the passphrase vault works only for interactive CLI commands."
    ),
    "spotify_client_id": "music-friend setup --spotify-client-id example-client-id",
    "spotify_connection": "music-friend connect spotify",
    "event_area": "music-friend setup --event-country US --event-postal 94110 --event-radius 50 --event-unit miles",
    "ticketmaster_key": "music-friend setup --ticketmaster-key-env TICKETMASTER_KEY",
}


def _doctor(
    application: MusicFriendApplication,
    config_store: object,
    structured: bool,
    stdout: TextIO,
    connector_factory: ConnectorFactory,
    credential_store_factory: CredentialStoreFactory,
    native_store_probe: NativeStoreProbe,
    now: Clock,
) -> int:
    """Report every onboarding blocker at once, locally, without reading secret values."""
    checks: dict[str, bool | None] = {
        "python": sys.version_info >= (3, 10),
        "credential_store": _probe(native_store_probe),
    }
    try:
        config: LocalConfig | None = _load_config(config_store)
    except Exception:
        config = None
    checks["spotify_client_id"] = config is not None and config.spotify_client_id is not None
    checks["event_area"] = config is not None and all(
        value is not None
        for value in (
            config.event_country_code,
            config.event_postal_code,
            config.event_radius,
            config.event_radius_unit,
        )
    )
    checks["spotify_connection"] = None
    checks["ticketmaster_key"] = None
    if checks["credential_store"] and config is not None:
        if checks["spotify_client_id"]:
            try:
                with _spotify_tokens(
                    config,
                    connector_factory=connector_factory,
                    credential_store_factory=credential_store_factory,
                ) as (_settings, tokens):
                    checks["spotify_connection"] = tokens.status().connected
            except Exception:
                checks["spotify_connection"] = False
        try:
            with _ticketmaster_client(
                connector_factory=connector_factory,
                credential_store_factory=credential_store_factory,
                now=now,
            ) as client:
                checks["ticketmaster_key"] = client.is_configured()
        except Exception:
            checks["ticketmaster_key"] = False
    release_sources = config.release_sources if config is not None else DEFAULT_RELEASE_SOURCES
    unmapped_artists = 0
    if "musicbrainz" in release_sources:
        unmapped_artists = sum(
            1
            for entry in application.list_watchlist(limit=500)
            if not any(ref.source == "musicbrainz" for ref in entry.artist.source_refs)
        )
    ready = all(value is True for value in checks.values())
    payload: dict[str, object] = {
        "status": "ready" if ready else "not_ready",
        "checks": {
            name: {
                "state": "ok" if value is True else "unchecked" if value is None else "failed",
                "remedy": None if value is True else _DOCTOR_REMEDIES[name],
            }
            for name, value in checks.items()
        },
        "release_source": {
            "sources": list(release_sources),
            "source": release_sources[0] if release_sources else "musicbrainz",
            "unmapped_artists": unmapped_artists,
            "remedy": (
                "Run 'music-friend refresh releases' to map artists"
                if "musicbrainz" in release_sources and unmapped_artists
                else None
            ),
        },
    }
    _emit(payload, structured, stdout, text=_doctor_text(payload))
    return 0 if ready else 5


def _doctor_text(payload: dict[str, object]) -> str:
    lines = [f"Music Friend doctor: {'ready' if payload['status'] == 'ready' else 'not ready'}"]
    checks = payload["checks"]
    assert isinstance(checks, dict)
    for name, result in checks.items():
        lines.append(f"[{result['state']}] {name}")
        if result["remedy"] is not None:
            lines.append(f"    {result['remedy']}")
    release_source = payload["release_source"]
    assert isinstance(release_source, dict)
    sources = release_source["sources"]
    assert isinstance(sources, list)
    if "musicbrainz" in sources:
        lines.append(
            f"release_source: musicbrainz ({release_source['unmapped_artists']} artists unmapped)"
        )
        if release_source["remedy"] is not None:
            lines.append(f"    {release_source['remedy']}")
    else:
        lines.append(f"release_source: {', '.join(sources) if sources else 'spotify'}")
    return "\n".join(lines)


def _refresh(
    kind: str,
    application: MusicFriendApplication,
    config: LocalConfig,
    refresh_runner: RefreshRunner | None,
    connector_factory: ConnectorFactory,
    credential_store_factory: CredentialStoreFactory,
    now: Clock,
    force: bool = False,
) -> object:
    if refresh_runner is not None:
        return refresh_runner(kind)
    checked_at = _checked_at(now)
    lock_path = Path(user_data_path("music-friend", appauthor=False)) / "refresh.lock"
    release_source_name = config.release_source or "musicbrainz"
    needs_catalog = kind in ("catalog", "all")
    needs_releases = kind in ("releases", "all")
    needs_spotify_source = needs_catalog or (needs_releases and release_source_name == "spotify")
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
                force=force,
                now=lambda: _checked_at(now),
            )
        with ExitStack() as stack:
            source: MusicSource | None = None
            if needs_spotify_source:
                source = stack.enter_context(
                    _spotify_source(
                        config,
                        connector_factory=connector_factory,
                        credential_store_factory=credential_store_factory,
                        now=now,
                    )
                )
            release_source: MusicSource | None = None
            if needs_releases:
                if release_source_name == "spotify":
                    # Same source, same name: refresh_once shares one paced wrapper.
                    release_source = source
                else:
                    # A musicbrainz-only or "all" refresh never needs a Spotify
                    # token session just for release discovery: identity mapping
                    # reads Spotify URLs already stored in the local catalog, it
                    # never calls Spotify live.
                    release_source = stack.enter_context(
                        _musicbrainz_source(connector_factory=connector_factory, now=now)
                    )
            return refresh_once(
                application,
                kind=kind,
                source_name="spotify",
                source=source,
                release_source=release_source,
                release_source_name=release_source_name if needs_releases else None,
                config=config,
                event_client=event_client,
                checked_at=checked_at,
                lock_path=lock_path,
                force=force,
                now=lambda: _checked_at(now),
            )


def _data_file_not_found(argument: str, stderr: TextIO) -> int:
    print(f"Music Friend could not find the file at {argument!r}.", file=stderr)
    return 1


def _data_destination_exists(argument: str, stderr: TextIO) -> int:
    print(f"Music Friend will not overwrite the existing file at {argument!r}.", file=stderr)
    return 1


def _data_archive_invalid(stderr: TextIO) -> int:
    print("Music Friend could not read the archive: it is not a valid export.", file=stderr)
    return 1


def _data_confirm(
    prompt: Prompt,
    message: str,
    expected: str,
    stderr: TextIO,
    *,
    yes_flag: bool = False,
    confirm_token: str | None = None,
) -> int | None:
    """Run a destructive-command confirmation.

    Returns ``None`` when confirmed (by flag, token, or prompt), or the exit
    code to return immediately when not, including when the prompt could not
    be read because stdin is not interactive.
    """
    if yes_flag:
        return None
    if confirm_token is not None:
        if confirm_token == expected:
            return None
        print("Confirmation was not accepted.", file=stderr)
        return 2
    try:
        answered = prompt(message)
    except EOFError:
        print(
            "Confirmation was not accepted: no terminal is attached to read it.",
            file=stderr,
        )
        return 2
    if answered != expected:
        print("Confirmation was not accepted.", file=stderr)
        return 2
    return None


def _data_command(
    argv: list[str],
    application: MusicFriendApplication,
    prompt: Prompt,
    structured: bool,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    if (
        len(argv) in {2, 3}
        and argv[0] == "import-spotify"
        and (len(argv) == 2 or argv[2] == "--dry-run")
    ):
        dry_run = len(argv) == 3
        try:
            result = application.import_spotify_history(Path(argv[1]), dry_run=dry_run)
        except FileNotFoundError:
            return _data_file_not_found(argv[1], stderr)
        except ValueError:
            return _data_archive_invalid(stderr)
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
        try:
            record_count = application.export_data(Path(argv[1])).record_count
        except FileExistsError:
            return _data_destination_exists(argv[1], stderr)
        except FileNotFoundError:
            return _data_file_not_found(argv[1], stderr)
        return _emit(
            {"records": record_count},
            structured,
            stdout,
            text="Data export complete.",
        )
    if len(argv) == 2 and argv[0] == "import":
        try:
            record_count = application.import_data(Path(argv[1])).record_count
        except FileNotFoundError:
            return _data_file_not_found(argv[1], stderr)
        except ValueError:
            return _data_archive_invalid(stderr)
        return _emit(
            {"records": record_count},
            structured,
            stdout,
            text="Data import complete.",
        )
    if len(argv) >= 2 and argv[0] == "restore":
        file_path = argv[1]
        yes_flag = False
        confirm_token = None
        extra_args = argv[2:]

        i = 0
        while i < len(extra_args):
            if extra_args[i] == "--yes":
                yes_flag = True
                i += 1
            elif extra_args[i] == "--confirm" and i + 1 < len(extra_args):
                confirm_token = extra_args[i + 1]
                i += 2
            else:
                print(_USAGE, end="", file=stderr)
                return 2

        rejected = _data_confirm(
            prompt,
            "Type RESTORE to continue: ",
            "RESTORE",
            stderr,
            yes_flag=yes_flag,
            confirm_token=confirm_token,
        )
        if rejected is not None:
            return rejected
        try:
            record_count = application.import_data(Path(file_path)).record_count
        except FileNotFoundError:
            return _data_file_not_found(file_path, stderr)
        except ValueError:
            return _data_archive_invalid(stderr)
        return _emit(
            {"records": record_count},
            structured,
            stdout,
            text="Data restore complete.",
        )
    if len(argv) >= 1 and argv[0] == "delete":
        yes_flag = False
        confirm_token = None
        extra_args = argv[1:]

        i = 0
        while i < len(extra_args):
            if extra_args[i] == "--yes":
                yes_flag = True
                i += 1
            elif extra_args[i] == "--confirm" and i + 1 < len(extra_args):
                confirm_token = extra_args[i + 1]
                i += 2
            else:
                print(_USAGE, end="", file=stderr)
                return 2

        rejected = _data_confirm(
            prompt,
            "Type DELETE to continue: ",
            "DELETE",
            stderr,
            yes_flag=yes_flag,
            confirm_token=confirm_token,
        )
        if rejected is not None:
            return rejected
        application.delete_data()
        return _emit({"status": "deleted"}, structured, stdout, text="Local data deleted.")
    print(_USAGE, end="", file=stderr)
    return 2


def _schedule_command(action: str, structured: bool, stdout: TextIO, stderr: TextIO) -> int:
    platform = _schedule_platform()
    root = Path.home()
    command = _scheduled_refresh_command()
    try:
        if action == "install":
            install_schedule(
                platform,
                user_root=root,
                command=command,
                interval_minutes=_DAILY_REFRESH_MINUTES,
            )
            return _emit({"status": "installed"}, structured, stdout, text="Schedule: installed.")
        if action == "remove":
            remove_schedule(platform, user_root=root)
            return _emit({"status": "removed"}, structured, stdout, text="Schedule: removed.")
        status: ScheduleStatus = schedule_status(
            platform, user_root=root, interval_minutes=_DAILY_REFRESH_MINUTES
        )
        payload: dict[str, object] = {
            "installed": status.installed,
            "active": status.active,
            "platform": status.platform.value,
            "interval_minutes": status.interval_minutes,
        }
        return _emit(
            payload,
            structured,
            stdout,
            text=(
                "Schedule: installed."
                if status.installed and status.active
                else "Schedule: installed but inactive."
                if status.installed
                else "Schedule: not installed."
            ),
        )
    except Exception:
        print("Music Friend could not complete the command.", file=stderr)
        return 1


def _scheduled_refresh_command() -> tuple[str, ...]:
    return (
        str(Path(sys.executable).absolute()),
        "-m",
        "music_friend.runtimes.cli",
        "refresh",
        "all",
        "--json",
    )


def _schedule_platform() -> SchedulePlatform:
    if sys.platform == "darwin":
        return SchedulePlatform.MACOS
    if sys.platform == "win32":
        return SchedulePlatform.WINDOWS
    return SchedulePlatform.LINUX


def _emit_refresh(value: object, structured: bool, stdout: TextIO) -> int:
    payload = _refresh_payload(value)
    _emit(payload, structured, stdout)
    status = payload["status"]
    if status == "partial":
        return 3
    if status in {"succeeded", "skipped"}:
        return 0
    return 1


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
        if value.run is None and value.skip_reason is not None:
            return {"status": "skipped", "reason": value.skip_reason}
        if value.run is not None:
            payload = _refresh_run(value.run)
            if value.skip_reason is not None:
                payload["events_skipped_reason"] = value.skip_reason
            if value.reason is not None:
                payload["reason"] = value.reason
            if value.retry_after is not None:
                payload["retry_after"] = value.retry_after
            if value.remaining is not None:
                payload["remaining"] = value.remaining
            return payload
    raise ValueError("refresh result is invalid")


def _catalog_status(application: MusicFriendApplication) -> dict[str, object]:
    latest = application.list_refresh_runs(limit=1)
    return {
        "status": "ready",
        "inbox": {"has_unread": bool(application.list_inbox_entries(InboxState.UNREAD, limit=1))},
        "latest_refresh": None if not latest else _refresh_run(latest[0]),
    }


def _diagnostics(
    application: MusicFriendApplication, config: LocalConfig, now: Clock
) -> dict[str, object]:
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
        "source_limits": {
            "spotify": _source_limit_diagnostics(application, "spotify", _checked_at(now))
        },
    }


def _source_limit_diagnostics(
    application: MusicFriendApplication, source: str, checked_at: datetime
) -> dict[str, object]:
    """Report the source's live cooldown state as of ``checked_at``.

    ``consecutive_limits`` is retained history: once ``retry_at`` has passed the reported
    ``state`` flips back to ``available`` for display, but the stored observation (and its
    ``consecutive_limits`` count) is left untouched. The next real limit response is still free
    to build on that history, and the counter only resets when a request actually succeeds.
    """
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
    expired = (
        observation is not None
        and observation.retry_at is not None
        and observation.retry_at <= checked_at
    )
    return {
        "state": ("available" if observation is None or expired else observation.state.value),
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


def _setup_command(
    argv: list[str],
    config_store: object,
    prompt: Prompt,
    secret_prompt: SecretPrompt,
    credential_store_factory: CredentialStoreFactory,
    structured: bool,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    """Handle setup with optional flags or interactive prompts."""
    try:
        prior = _load_config(config_store)
    except Exception:
        print("Music Friend could not complete the command.", file=stderr)
        return 1

    # Parse flags
    flags: dict[str, str | None] = {}
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in {
            "--event-country",
            "--event-postal",
            "--event-radius",
            "--event-unit",
            "--spotify-client-id",
            "--release-sources",
        }:
            if i + 1 >= len(argv):
                print(_USAGE, end="", file=stderr)
                return 2
            flags[arg[2:].replace("-", "_")] = argv[i + 1]
            i += 2
        elif arg.startswith("--clear-"):
            field = arg[8:]
            if field not in {
                "event-country",
                "event-postal",
                "event-radius",
                "event-unit",
                "spotify-client-id",
            }:
                print(_USAGE, end="", file=stderr)
                return 2
            flags[f"clear_{field.replace('-', '_')}"] = "true"
            i += 1
        elif arg in {
            "--ticketmaster-key-env",
            "--ticketmaster-key-file",
            "--ticketmaster-key-stdin",
        }:
            if arg == "--ticketmaster-key-stdin":
                flags["ticketmaster_key_stdin"] = "true"
                i += 1
            else:
                if i + 1 >= len(argv):
                    print(_USAGE, end="", file=stderr)
                    return 2
                flags[arg[2:].replace("-", "_")] = argv[i + 1]
                i += 2
        else:
            print(_USAGE, end="", file=stderr)
            return 2

    try:
        # If no flags, use interactive prompts
        if not flags:
            configured = _setup_config(prior, prompt)
            key_action = _setup_key_action(
                secret_prompt("Ticketmaster API key (blank to preserve, - to remove): ")
            )
        else:
            # Non-interactive setup with flags
            configured = _setup_config_from_flags(prior, flags)
            key_action = _setup_key_from_flags(flags)
    except Exception as e:
        print(f"Music Friend could not complete the command: {e}", file=stderr)
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

    result: dict[str, object] = {
        "status": "setup_complete",
        "spotify_client_id": configured.spotify_client_id,
        "event_country_code": configured.event_country_code,
        "event_postal_code": configured.event_postal_code,
        "event_radius": configured.event_radius,
        "event_radius_unit": configured.event_radius_unit,
        "release_sources": list(configured.release_sources),
    }
    return _emit(result, structured, stdout, text="Music Friend setup complete.")


def _setup_config_from_flags(prior: LocalConfig, flags: dict[str, str | None]) -> LocalConfig:
    """Apply flag-based changes to configuration."""
    spotify_client_id = prior.spotify_client_id
    event_country_code = prior.event_country_code
    event_postal_code = prior.event_postal_code
    event_radius = prior.event_radius
    event_radius_unit = prior.event_radius_unit
    release_sources = prior.release_sources

    # Handle clears
    if flags.get("clear_spotify_client_id") == "true":
        spotify_client_id = None
    if flags.get("clear_event_country") == "true":
        event_country_code = None
    if flags.get("clear_event_postal") == "true":
        event_postal_code = None
    if flags.get("clear_event_radius") == "true":
        event_radius = None
    if flags.get("clear_event_unit") == "true":
        event_radius_unit = None

    # Handle sets
    if "spotify_client_id" in flags and flags["spotify_client_id"] is not None:
        spotify_client_id = _setup_text(
            prior.spotify_client_id,
            flags["spotify_client_id"],
            normalize=lambda value: value.strip(),
        )
    if "release_sources" in flags and flags["release_sources"] is not None:
        release_sources = _setup_release_sources(prior.release_sources, flags["release_sources"])

    if (
        "event_country" in flags
        or "event_postal" in flags
        or "event_radius" in flags
        or "event_unit" in flags
    ):
        country = event_country_code
        postal = event_postal_code
        radius = event_radius
        unit = event_radius_unit

        if "event_country" in flags and flags["event_country"] is not None:
            country = flags["event_country"].strip().upper()
        if "event_postal" in flags and flags["event_postal"] is not None:
            postal = flags["event_postal"].strip()
        if "event_radius" in flags and flags["event_radius"] is not None:
            try:
                numeric = float(flags["event_radius"].strip())
                radius = int(numeric) if numeric.is_integer() else numeric
            except ValueError:
                raise ValueError("event_radius is invalid")
        if "event_unit" in flags and flags["event_unit"] is not None:
            normalized = flags["event_unit"].strip().lower()
            if normalized == "miles":
                unit = "miles"
            elif normalized == "kilometers":
                unit = "kilometers"
            else:
                raise ValueError("event_radius_unit is invalid")

        if country is None or postal is None:
            if "event_country" in flags or "event_postal" in flags:
                if country is None or postal is None:
                    raise ValueError("event area is incomplete")
        if unit is None and ("event_unit" in flags or radius is not None):
            unit = "miles"
        if radius is None and "event_radius" in flags and unit is not None:
            radius = 80 if unit == "kilometers" else 50

        event_country_code = country
        event_postal_code = postal
        event_radius = radius
        event_radius_unit = unit

    return LocalConfig(
        spotify_client_id=spotify_client_id,
        event_country_code=event_country_code,
        event_postal_code=event_postal_code,
        event_radius=event_radius,
        event_radius_unit=event_radius_unit,
        release_sources=release_sources,
    )


def _setup_key_from_flags(flags: dict[str, str | None]) -> str | None | _Preserve:
    """Get Ticketmaster key from flags."""

    if "ticketmaster_key_stdin" in flags:
        # Read from stdin (must be available)
        try:
            key = sys.stdin.read().strip()
            if not key or len(key) > 4096:
                raise ValueError("Ticketmaster key is invalid")
            return key
        except Exception:
            raise ValueError("Could not read Ticketmaster key from stdin")

    if "ticketmaster_key_env" in flags:
        env_var = flags["ticketmaster_key_env"]
        if env_var is None:
            raise ValueError("Environment variable name required")
        key = os.environ.get(env_var)
        if key is None:
            raise ValueError(f"Environment variable {env_var} not set")
        if not key or len(key) > 4096:
            raise ValueError("Ticketmaster key is invalid")
        return key

    if "ticketmaster_key_file" in flags:
        file_path = flags["ticketmaster_key_file"]
        if file_path is None:
            raise ValueError("File path required")
        try:
            path = Path(file_path)
            if not path.exists():
                raise ValueError(f"File not found: {file_path}")
            stat_info = path.stat()
            if stat.S_IMODE(stat_info.st_mode) != 0o600:
                raise ValueError(
                    f"File must have permissions 0o600 (chmod 600), got {oct(stat.S_IMODE(stat_info.st_mode))}"
                )
            key = path.read_text(encoding="utf-8").strip()
            if not key or len(key) > 4096:
                raise ValueError("Ticketmaster key is invalid")
            return key
        except ValueError:
            raise
        except Exception:
            raise ValueError(f"Could not read Ticketmaster key from {file_path}")

    return _PRESERVE


def _connect_command(
    argv: list[str],
    config: LocalConfig,
    structured: bool,
    stdout: TextIO,
    stderr: TextIO,
    connector_factory: ConnectorFactory,
    credential_store_factory: CredentialStoreFactory,
    browser_opener: BrowserOpener,
    authorizer_factory: AuthorizerFactory,
) -> int:
    """Handle connect spotify with optional flags."""
    if argv and argv[0] not in {"--json"}:
        print(_USAGE, end="", file=stderr)
        return 2

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
        payload: dict[str, object] = {"status": "connected"}
        return _emit(payload, structured, stdout, text="Music Friend connected to Spotify.")
    except _ConnectionFailed:
        print("Music Friend could not connect to Spotify.", file=stderr)
        return 1
    except _ProviderNotConfigured:
        print(_PROVIDER_NOT_CONFIGURED_MESSAGE, file=stderr)
        return 1
    except Exception:
        print("Music Friend could not complete the command.", file=stderr)
        return 1


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
        line = f"Refresh {payload['kind']}: {payload['status']}."
        if payload.get("status") == "partial" and type(payload.get("reason")) is str:
            line = f"{line} reason={payload['reason']}"
            if type(payload.get("retry_after")) is str:
                line = f"{line} retry_after={payload['retry_after']}"
            if type(payload.get("remaining")) is int:
                line = f"{line} remaining={payload['remaining']}"
        return line
    if type(payload.get("status")) is str:
        connection = payload.get("connection")
        suffix = f" (Spotify: {connection})" if type(connection) is str else ""
        events = payload.get("events")
        if isinstance(events, dict) and type(events.get("ready")) is bool:
            suffix = suffix.removesuffix(")") + (
                f"; Events: {'ready' if events['ready'] else 'not ready'})"
            )
        if type(payload.get("mcp_ready")) is bool:
            suffix = suffix.removesuffix(")") + (
                "; MCP: ready)"
                if payload["mcp_ready"]
                else "; MCP: needs a native credential store, run music-friend doctor)"
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
