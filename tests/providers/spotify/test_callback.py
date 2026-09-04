from __future__ import annotations

from collections.abc import Mapping
from types import SimpleNamespace

import pytest

from music_friend.providers.spotify import callback as callback_module
from music_friend.providers.spotify.callback import (
    _CALLBACK_PAGE,
    _CallbackHandler,
    _CallbackOutcome,
    _CallbackStatus,
    _new_callback_server,
    _parse_callback_request,
    _parse_callback_target,
    _serve_one,
)

REDIRECT_URI = "http://127.0.0.1:43210/callback"
EXPECTED_STATE = "expected-state"


class _FakeServer:
    def __init__(self, outcome: _CallbackOutcome | None) -> None:
        self._outcome = outcome
        self.handle_count = 0
        self.closed = False
        self.timeout: float | None = None

    def handle_request(self) -> None:
        self.handle_count += 1

    def server_close(self) -> None:
        self.closed = True


def _target(query: str, *, path: str = "/callback") -> str:
    return f"{path}?{query}"


def test_origin_form_callback_accepts_one_nonempty_code() -> None:
    outcome = _parse_callback_target(
        _target("code=authorization-code&state=expected-state"),
        redirect_uri=REDIRECT_URI,
        expected_state=EXPECTED_STATE,
    )

    assert outcome._status is _CallbackStatus.SUCCESS
    assert outcome._code == "authorization-code"


@pytest.mark.parametrize(
    "ubi_value",
    ("opaque-browser-value", "%61" * 1024),
    ids=("opaque", "percent-decoded-boundary"),
)
def test_success_callback_accepts_and_discards_one_bounded_ubi_value(ubi_value: str) -> None:
    outcome = _parse_callback_target(
        _target(f"code=authorization-code&state=expected-state&ubi={ubi_value}"),
        redirect_uri=REDIRECT_URI,
        expected_state=EXPECTED_STATE,
    )

    assert outcome._status is _CallbackStatus.SUCCESS
    assert outcome._code == "authorization-code"
    assert repr(outcome) == "<_CallbackOutcome status=SUCCESS>"


@pytest.mark.parametrize(
    "ubi_query",
    (
        "ubi",
        "ubi=",
        "ubi=" + "%61" * 1025,
        "ubi=first&ubi=second",
    ),
    ids=("missing-value", "empty", "oversized-after-percent-decoding", "duplicate"),
)
def test_success_callback_rejects_invalid_ubi_values(ubi_query: str) -> None:
    outcome = _parse_callback_target(
        _target(f"code=value&state=expected-state&{ubi_query}"),
        redirect_uri=REDIRECT_URI,
        expected_state=EXPECTED_STATE,
    )

    assert outcome._status is _CallbackStatus.INVALID


@pytest.mark.parametrize(
    "query",
    (
        "code=value&state=expected-state&ubi=%FF",
        "code=value&state=expected-state&ubi=%E2%82",
        "code=%FF&state=expected-state",
        "code=value&state=%FF",
    ),
    ids=("ubi-invalid-byte", "ubi-truncated-multibyte", "code", "state"),
)
def test_callback_rejects_malformed_percent_decoded_utf8(query: str) -> None:
    outcome = _parse_callback_target(
        _target(query),
        redirect_uri=REDIRECT_URI,
        expected_state=EXPECTED_STATE,
    )

    assert outcome._status is _CallbackStatus.INVALID


def test_absolute_form_callback_requires_the_exact_origin_and_path() -> None:
    outcome = _parse_callback_target(
        "http://127.0.0.1:43210/callback?code=authorization-code&state=expected-state",
        redirect_uri=REDIRECT_URI,
        expected_state=EXPECTED_STATE,
    )

    assert outcome._status is _CallbackStatus.SUCCESS


@pytest.mark.parametrize("host_name", ("Host", "host"))
def test_origin_form_request_requires_the_exact_host_authority(host_name: str) -> None:
    outcome = _parse_callback_request(
        method="GET",
        target=_target("code=value&state=expected-state"),
        headers={host_name: "127.0.0.1:43210"},
        body=b"",
        redirect_uri=REDIRECT_URI,
        expected_state=EXPECTED_STATE,
    )

    assert outcome._status is _CallbackStatus.SUCCESS


@pytest.mark.parametrize(
    ("target", "status"),
    (
        (_target("error=access_denied&state=expected-state"), _CallbackStatus.DENIED),
        (_target("code=value&state=wrong-state"), _CallbackStatus.INVALID),
        (_target("code=value&state=expected-state", path="/wrong"), _CallbackStatus.INVALID),
        ("not-a-request-target", _CallbackStatus.INVALID),
    ),
    ids=("denied", "wrong-state", "wrong-path", "malformed"),
)
def test_callback_outcomes_are_closed_and_redacted(target: str, status: _CallbackStatus) -> None:
    outcome = _parse_callback_target(
        target,
        redirect_uri=REDIRECT_URI,
        expected_state=EXPECTED_STATE,
    )

    assert outcome._status is status
    assert repr(outcome) == f"<_CallbackOutcome status={status.name}>"


@pytest.mark.parametrize(
    "target",
    (
        _target("code=first&code=second&state=expected-state"),
        _target("code=value&state=expected-state&state=expected-state"),
        _target("code=&state=expected-state"),
        _target("code=value&error=access_denied&state=expected-state"),
        _target("error=other&state=expected-state"),
        _target("error=access_denied&state=expected-state&ubi=value"),
        _target("code=value&state=expected-state&extra=value"),
        "http://127.0.0.1:43211/callback?code=value&state=expected-state",
        "http://localhost:43210/callback?code=value&state=expected-state",
        "http://user@127.0.0.1:43210/callback?code=value&state=expected-state",
        "http://127.0.0.1:43210/callback?code=value&state=expected-state#fragment",
        "/%63allback?code=value&state=expected-state",
        "/callback%2fextra?code=value&state=expected-state",
        "/callback?code=value&state=expected-state%ZZ",
        "/callback?code=value&state=expected-state\n",
        "/callback?code=value&state=expected-state\r",
        "/callback?code=value&state=expected-state\t",
        "/callback?code=value&state=expected-state\x00",
    ),
    ids=(
        "duplicate-code",
        "duplicate-state",
        "empty-code",
        "code-and-denial",
        "unknown-denial",
        "denial-with-ubi",
        "extra-key",
        "wrong-port",
        "localhost",
        "userinfo",
        "fragment",
        "encoded-path",
        "encoded-separator",
        "bad-percent-escape",
        "newline",
        "carriage-return",
        "tab",
        "nul",
    ),
)
def test_callback_target_rejects_ambiguous_or_confused_forms(target: str) -> None:
    outcome = _parse_callback_target(
        target,
        redirect_uri=REDIRECT_URI,
        expected_state=EXPECTED_STATE,
    )

    assert outcome._status is _CallbackStatus.INVALID


@pytest.mark.parametrize(
    ("method", "headers", "body"),
    (
        ("POST", {}, b""),
        ("GET", {"X-Oversized": "x" * 8193}, b""),
        ("GET", {}, b"x"),
        ("GET", {"Content-Length": "1"}, b""),
    ),
    ids=("non-get", "headers", "body", "declared-body"),
)
def test_callback_request_rejects_methods_headers_and_bodies(
    method: str, headers: Mapping[str, str], body: bytes
) -> None:
    request_headers = {"Host": "127.0.0.1:43210", **headers}
    outcome = _parse_callback_request(
        method=method,
        target=_target("code=value&state=expected-state"),
        headers=request_headers,
        body=body,
        redirect_uri=REDIRECT_URI,
        expected_state=EXPECTED_STATE,
    )

    assert outcome._status is _CallbackStatus.INVALID


@pytest.mark.parametrize(
    "headers",
    (
        {},
        {"Host": "localhost:43210"},
        {"Host": "127.0.0.1"},
        {"Host": "127.0.0.1:43211"},
        {"Host": "127.0.0.1:43210", "host": "127.0.0.1:43210"},
        {"Host": "127.0.0.1:43210", "Transfer-Encoding": "chunked"},
        {"Host": "127.0.0.1:43210", "transfer-encoding": "identity"},
    ),
    ids=(
        "missing-host",
        "localhost",
        "missing-port",
        "wrong-port",
        "duplicate-host",
        "chunked",
        "transfer-encoding",
    ),
)
def test_callback_request_rejects_wrong_authority_and_transfer_encoding(
    headers: Mapping[str, str],
) -> None:
    outcome = _parse_callback_request(
        method="GET",
        target=_target("code=value&state=expected-state"),
        headers=headers,
        body=b"",
        redirect_uri=REDIRECT_URI,
        expected_state=EXPECTED_STATE,
    )

    assert outcome._status is _CallbackStatus.INVALID


@pytest.mark.parametrize(
    "outcome",
    ("success", "denied", "wrong_state", "wrong_path", "malformed", "timeout"),
)
def test_first_request_always_closes_listener(outcome: str) -> None:
    completed_outcomes = {
        "success": _CallbackOutcome.success("code"),
        "denied": _CallbackOutcome.denied(),
        "wrong_state": _CallbackOutcome.invalid(),
        "wrong_path": _CallbackOutcome.invalid(),
        "malformed": _CallbackOutcome.invalid(),
        "timeout": None,
    }
    fake_server = _FakeServer(completed_outcomes[outcome])

    completed = _serve_one(fake_server)

    assert fake_server.handle_count <= 1
    assert fake_server.closed is True
    assert fake_server.timeout == 180.0
    completed_outcome = completed_outcomes[outcome]
    expected = _CallbackStatus.TIMEOUT if completed_outcome is None else completed_outcome._status
    assert completed._status is expected


def test_listener_closes_when_request_handling_raises() -> None:
    class RaisingServer(_FakeServer):
        def handle_request(self) -> None:
            super().handle_request()
            raise OSError("request-canary")

    fake_server = RaisingServer(None)

    outcome = _serve_one(fake_server)

    assert outcome._status is _CallbackStatus.INVALID
    assert fake_server.handle_count == 1
    assert fake_server.closed is True


def test_callback_page_is_static_bounded_and_hardened() -> None:
    assert len(_CALLBACK_PAGE) < 1024
    assert b"authorization-code" not in _CALLBACK_PAGE
    assert b"expected-state" not in _CALLBACK_PAGE
    assert b"script" not in _CALLBACK_PAGE.lower()


def test_callback_response_is_no_store_and_csp_hardened() -> None:
    class _Writer:
        def __init__(self) -> None:
            self.body = b""

        def write(self, body: bytes) -> None:
            self.body += body

    class _ResponseRecorder:
        def __init__(self) -> None:
            self.statuses: list[int] = []
            self.headers: list[tuple[str, str]] = []
            self.wfile = _Writer()
            self.ended = False

        def send_response(self, status: int) -> None:
            self.statuses.append(status)

        def send_header(self, name: str, value: str) -> None:
            self.headers.append((name, value))

        def end_headers(self) -> None:
            self.ended = True

    recorder = _ResponseRecorder()

    _CallbackHandler._respond(recorder)  # type: ignore[arg-type]

    assert recorder.statuses == [200]
    assert ("Cache-Control", "no-store") in recorder.headers
    assert (
        "Content-Security-Policy",
        "default-src 'none'; frame-ancestors 'none'",
    ) in recorder.headers
    assert recorder.ended is True
    assert recorder.wfile.body == _CALLBACK_PAGE


def test_callback_handler_never_logs_request_lines(capsys: pytest.CaptureFixture[str]) -> None:
    _CallbackHandler.log_message(object(), "request-line-canary")  # type: ignore[arg-type]

    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


class _HandlerRecorder:
    def __init__(self) -> None:
        self.server = SimpleNamespace(
            _redirect_uri=REDIRECT_URI,
            _expected_state=EXPECTED_STATE,
            _outcome=None,
        )
        self.command = "GET"
        self.path = _target("code=value&state=expected-state")
        self.headers = {"Host": "127.0.0.1:43210"}
        self.responses = 0

    def _respond(self) -> None:
        self.responses += 1

    def _reject_non_get(self) -> None:
        _CallbackHandler._reject_non_get(self)  # type: ignore[arg-type]


def test_callback_handler_records_get_outcome_and_rejects_every_write_method() -> None:
    """Catches HTTP handler methods bypassing the closed callback parser or response path."""
    get = _HandlerRecorder()
    _CallbackHandler.do_GET(get)  # type: ignore[arg-type]
    assert get.server._outcome._status is _CallbackStatus.SUCCESS
    assert get.responses == 1

    for method in (_CallbackHandler.do_POST, _CallbackHandler.do_PUT, _CallbackHandler.do_PATCH):
        rejected = _HandlerRecorder()
        method(rejected)  # type: ignore[arg-type]
        assert rejected.server._outcome._status is _CallbackStatus.INVALID
        assert rejected.responses == 1


def test_new_callback_server_binds_only_loopback_and_records_exact_redirect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches callback listener construction broadening its bind address or losing state."""

    class FakeHTTPServer:
        def __init__(self, address: tuple[str, int], handler: object) -> None:
            assert address == ("127.0.0.1", 0)
            assert handler is _CallbackHandler
            self.server_address = (address[0], 43210)
            self.closed = False

        def server_close(self) -> None:
            self.closed = True

    monkeypatch.setattr(callback_module, "HTTPServer", FakeHTTPServer)
    server = _new_callback_server(0, expected_state=EXPECTED_STATE)
    assert server.server_address[0] == "127.0.0.1"
    assert server._redirect_uri == REDIRECT_URI
    assert server._expected_state == EXPECTED_STATE
    assert server._outcome is None
