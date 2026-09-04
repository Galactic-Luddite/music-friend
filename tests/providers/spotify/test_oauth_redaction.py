from __future__ import annotations

import json
import logging
import sys
from dataclasses import asdict

import httpx
import pytest

from music_friend.providers import Capability
from music_friend.providers.credentials import CredentialKey
from music_friend.providers.spotify import callback as spotify_callback
from music_friend.providers.spotify.callback import _CALLBACK_PAGE, _CallbackOutcome
from music_friend.providers.spotify.config import SpotifySettings
from music_friend.providers.spotify.oauth import (
    AuthorizationMode,
    AuthorizationResult,
    SpotifyAuthorization,
    _AuthorizationAttempt,
    _pkce_verifier,
    _state_token,
)
from music_friend.providers.spotify.tokens import SpotifyTokenManager
from music_friend.providers.spotify.transport import SpotifyTransport, _TraceEntry

URL_CANARY = "url-client-canary"
STATE_CANARY = _state_token(lambda length: b"s" * length)
VERIFIER_CANARY = _pkce_verifier(lambda length: b"v" * length)
CODE_CANARY = "authorization-code-canary"
ACCESS_CANARY = "access-token-canary"
REFRESH_CANARY = "refresh-token-canary"
CANARIES = (
    URL_CANARY,
    STATE_CANARY,
    VERIFIER_CANARY,
    CODE_CANARY,
    ACCESS_CANARY,
    REFRESH_CANARY,
)
SURFACE_NAMES = (
    "stdout",
    "stderr",
    "logging",
    "attempt-repr",
    "authorization-repr",
    "public-result",
    "exception-string",
    "exception-dict",
    "callback-page",
    "transport-trace",
)


class _OneServer:
    server_address = ("127.0.0.1", 43210)

    def __init__(self) -> None:
        self.timeout: float | None = None
        self._outcome = _CallbackOutcome.success(CODE_CANARY)

    def handle_request(self) -> None:
        return None

    def server_close(self) -> None:
        return None


class _RaisingServer(_OneServer):
    def handle_request(self) -> None:
        raise OSError("|".join(CANARIES))


class _MemoryStore:
    def __init__(self) -> None:
        self.values: dict[CredentialKey, str] = {}

    def save(self, key: CredentialKey, value: str) -> None:
        self.values[key] = value

    def load(self, key: CredentialKey) -> str | None:
        return self.values.get(key)

    def delete(self, key: CredentialKey) -> None:
        self.values.pop(key, None)


def _tokens(settings: SpotifySettings, transport: SpotifyTransport) -> SpotifyTokenManager:
    return SpotifyTokenManager(settings=settings, transport=transport, store=_MemoryStore())


def _assert_redacted(surface: str) -> None:
    for canary in CANARIES:
        assert canary not in surface


def test_authorization_redacts_every_observable_surface(
    capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "access_" + "token": ACCESS_CANARY,
                "refresh_" + "token": REFRESH_CANARY,
                "token_type": "Bearer",
                "expires_in": 3600,
            },
        )

    transport = SpotifyTransport(httpx.MockTransport(respond))
    browser_urls: list[str] = []
    random_values = iter((b"v" * 32, b"s" * 32))
    authorizer = SpotifyAuthorization(
        settings=SpotifySettings(URL_CANARY, "http://127.0.0.1:43210/callback"),
        tokens=_tokens(SpotifySettings(URL_CANARY, "http://127.0.0.1:43210/callback"), transport),
        browser_opener=lambda url: not browser_urls.append(url),
        random_bytes=lambda _length: next(random_values),
        _server_factory=lambda _port, *, expected_state: _OneServer(),
    )
    attempt = _AuthorizationAttempt(
        verifier=VERIFIER_CANARY,
        state=STATE_CANARY,
        redirect_uri="http://127.0.0.1:43210/callback",
        authorization_url=f"https://accounts.spotify.com/authorize?client_id={URL_CANARY}",
    )
    with pytest.raises(RuntimeError) as raised:
        attempt.complete()

    with caplog.at_level(logging.DEBUG):
        result = authorizer.authorize(
            frozenset({Capability.HEALTH}),
            mode=AuthorizationMode.FIXED_LOOPBACK,
        )
    captured = capsys.readouterr()
    assert len(browser_urls) == 1
    assert URL_CANARY in browser_urls[0]
    surfaces = {
        "stdout": captured.out,
        "stderr": captured.err,
        "logging": caplog.text,
        "attempt-repr": repr(attempt),
        "authorization-repr": repr(authorizer),
        "public-result": json.dumps(asdict(result), default=str, sort_keys=True),
        "exception-string": str(raised.value),
        "exception-dict": json.dumps(vars(raised.value), default=str, sort_keys=True),
        "callback-page": _CALLBACK_PAGE.decode("utf-8"),
        "transport-trace": repr(transport.trace),
    }

    assert tuple(surfaces) == SURFACE_NAMES
    for surface in surfaces.values():
        _assert_redacted(surface)


def _assert_exposure_detected(recorded: str) -> None:
    with pytest.raises(AssertionError):
        _assert_redacted(recorded)


def test_stdout_recorder_positive_control(capsys: pytest.CaptureFixture[str]) -> None:
    print(URL_CANARY)

    _assert_exposure_detected(capsys.readouterr().out)


def test_stderr_recorder_positive_control(capsys: pytest.CaptureFixture[str]) -> None:
    print(STATE_CANARY, file=sys.stderr)

    _assert_exposure_detected(capsys.readouterr().err)


def test_logging_recorder_positive_control(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        logging.getLogger("music_friend.spotify.redaction_control").warning(VERIFIER_CANARY)

    _assert_exposure_detected(caplog.text)


def test_repr_recorder_positive_control() -> None:
    class Exposed:
        def __repr__(self) -> str:
            return CODE_CANARY

    _assert_exposure_detected(repr(Exposed()))


def test_public_result_recorder_positive_control() -> None:
    exposed = AuthorizationResult(False, frozenset({ACCESS_CANARY}))  # type: ignore[arg-type]
    recorded = json.dumps(asdict(exposed), default=str, sort_keys=True)

    _assert_exposure_detected(recorded)


def test_exception_string_recorder_positive_control() -> None:
    try:
        raise RuntimeError(REFRESH_CANARY)
    except RuntimeError as raised:
        recorded = str(raised)

    _assert_exposure_detected(recorded)


def test_exception_dictionary_recorder_positive_control() -> None:
    raised = RuntimeError("safe exception text")
    raised.redaction_detail = ACCESS_CANARY
    recorded = json.dumps(vars(raised), default=str, sort_keys=True)

    assert ACCESS_CANARY not in str(raised)
    _assert_exposure_detected(recorded)


def test_callback_page_recorder_positive_control(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(spotify_callback, "_CALLBACK_PAGE", URL_CANARY.encode("ascii"))

    _assert_exposure_detected(spotify_callback._CALLBACK_PAGE.decode("utf-8"))


def test_transport_trace_recorder_positive_control() -> None:
    with SpotifyTransport(httpx.MockTransport(lambda _request: httpx.Response(200))) as transport:
        transport._trace.append(_TraceEntry(STATE_CANARY, (), ()))

        _assert_exposure_detected(repr(transport.trace))


@pytest.mark.parametrize("boundary", ("browser", "listener", "reader", "transport"))
def test_dependency_exception_canaries_never_escape(
    boundary: str,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    diagnostic = "|".join(CANARIES)

    def respond(request: httpx.Request) -> httpx.Response:
        if boundary == "transport":
            raise httpx.ConnectError(diagnostic, request=request)
        return httpx.Response(
            200,
            json={
                "access_" + "token": ACCESS_CANARY,
                "refresh_" + "token": REFRESH_CANARY,
                "token_type": "Bearer",
                "expires_in": 3600,
            },
        )

    def open_browser(_url: str) -> bool:
        if boundary == "browser":
            raise RuntimeError(diagnostic)
        return True

    def read_callback() -> str:
        raise RuntimeError(diagnostic)

    random_values = iter((b"v" * 32, b"s" * 32))
    manual = boundary == "reader"
    settings = SpotifySettings(URL_CANARY, "http://127.0.0.1:43210/callback")
    transport = SpotifyTransport(httpx.MockTransport(respond))
    authorizer = SpotifyAuthorization(
        settings=settings,
        tokens=_tokens(settings, transport),
        browser_opener=open_browser,
        random_bytes=lambda _length: next(random_values),
        _server_factory=lambda _port, *, expected_state: (
            _RaisingServer() if boundary == "listener" else _OneServer()
        ),
    )

    with caplog.at_level(logging.DEBUG):
        result = authorizer.authorize(
            frozenset({Capability.HEALTH}),
            mode=AuthorizationMode.MANUAL if manual else AuthorizationMode.FIXED_LOOPBACK,
            callback_reader=read_callback if manual else None,
        )
    captured = capsys.readouterr()
    observable = "|".join(
        (
            captured.out,
            captured.err,
            caplog.text,
            repr(result),
            repr(authorizer),
            repr(transport.trace),
        )
    )

    assert result.authorized is False
    _assert_redacted(observable)
