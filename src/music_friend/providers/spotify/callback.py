"""Single-use loopback callback handling for Spotify authorization."""

# Adapted from https://github.com/fabioc-aloha/spotify-skill; modified for Music Friend.

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Protocol, cast
from urllib.parse import parse_qsl, urlsplit

_CALLBACK_DEADLINE_SECONDS = 180.0
_MAX_REQUEST_TARGET_BYTES = 8192
_MAX_HEADER_BYTES = 8192
_MAX_UBI_BYTES = 1024
_BAD_PERCENT_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})")
_CONTROL_CHARACTER = re.compile(r"[\x00-\x1f\x7f]")
_CALLBACK_PAGE = (
    b"<!doctype html><html><head><meta charset=utf-8>"
    b"<meta name=viewport content='width=device-width,initial-scale=1'>"
    b"<title>Music Friend</title></head><body>"
    b"<p>Authorization response received. You may return to Music Friend.</p>"
    b"</body></html>"
)


class _CallbackStatus(str, Enum):
    SUCCESS = "success"
    DENIED = "denied"
    INVALID = "invalid"
    TIMEOUT = "timeout"


@dataclass(frozen=True, slots=True, repr=False)
class _CallbackOutcome:
    _status: _CallbackStatus
    _code: str | None = None

    def __repr__(self) -> str:
        return f"<_CallbackOutcome status={self._status.name}>"

    @classmethod
    def success(cls, code: str) -> _CallbackOutcome:
        return cls(_CallbackStatus.SUCCESS, code)

    @classmethod
    def denied(cls) -> _CallbackOutcome:
        return cls(_CallbackStatus.DENIED)

    @classmethod
    def invalid(cls) -> _CallbackOutcome:
        return cls(_CallbackStatus.INVALID)

    @classmethod
    def timeout(cls) -> _CallbackOutcome:
        return cls(_CallbackStatus.TIMEOUT)


class _CallbackServer(Protocol):
    timeout: float | None
    _outcome: _CallbackOutcome | None

    def handle_request(self) -> None: ...

    def server_close(self) -> None: ...


def _parse_callback_target(
    target: str,
    *,
    redirect_uri: str,
    expected_state: str,
    require_absolute: bool = False,
) -> _CallbackOutcome:
    if (
        not isinstance(target, str)
        or not target
        or len(target.encode("utf-8")) > _MAX_REQUEST_TARGET_BYTES
        or "\\" in target
        or _BAD_PERCENT_ESCAPE.search(target) is not None
        or _CONTROL_CHARACTER.search(target) is not None
    ):
        return _CallbackOutcome.invalid()
    try:
        expected = urlsplit(redirect_uri)
        parsed = urlsplit(target)
        expected_port = expected.port
        parsed_port = parsed.port
    except (ValueError, UnicodeError):
        return _CallbackOutcome.invalid()
    if (
        expected.scheme != "http"
        or expected.hostname != "127.0.0.1"
        or expected.username is not None
        or expected.password is not None
        or expected_port is None
        or expected.path != "/callback"
        or expected.query
        or expected.fragment
    ):
        return _CallbackOutcome.invalid()
    is_absolute = bool(parsed.scheme or parsed.netloc)
    if require_absolute and not is_absolute:
        return _CallbackOutcome.invalid()
    if is_absolute:
        if (
            parsed.scheme != expected.scheme
            or parsed.netloc != expected.netloc
            or parsed.hostname != expected.hostname
            or parsed_port != expected_port
            or parsed.username is not None
            or parsed.password is not None
        ):
            return _CallbackOutcome.invalid()
    elif not target.startswith("/") or target.startswith("//"):
        return _CallbackOutcome.invalid()
    if parsed.path != expected.path or parsed.fragment:
        return _CallbackOutcome.invalid()
    try:
        pairs = parse_qsl(
            parsed.query,
            keep_blank_values=True,
            strict_parsing=True,
            errors="strict",
            max_num_fields=4,
        )
    except (ValueError, UnicodeError):
        return _CallbackOutcome.invalid()
    values: dict[str, str] = {}
    for key, value in pairs:
        if key in values:
            return _CallbackOutcome.invalid()
        values[key] = value
    if values.get("state") != expected_state:
        return _CallbackOutcome.invalid()
    if set(values) in ({"code", "state"}, {"code", "state", "ubi"}):
        code = values["code"]
        if not code or len(code) > 4096:
            return _CallbackOutcome.invalid()
        ubi = values.get("ubi")
        if ubi is not None and (not ubi or len(ubi.encode("utf-8")) > _MAX_UBI_BYTES):
            return _CallbackOutcome.invalid()
        return _CallbackOutcome.success(code)
    if values == {"error": "access_denied", "state": expected_state}:
        return _CallbackOutcome.denied()
    return _CallbackOutcome.invalid()


def _parse_callback_request(
    *,
    method: str,
    target: str,
    headers: Mapping[str, str],
    body: bytes,
    redirect_uri: str,
    expected_state: str,
) -> _CallbackOutcome:
    try:
        expected_authority = urlsplit(redirect_uri).netloc
        normalized_headers: dict[str, str] = {}
        header_bytes = 0
        for name, value in headers.items():
            normalized = name.lower()
            if normalized in normalized_headers:
                return _CallbackOutcome.invalid()
            normalized_headers[normalized] = value
            header_bytes += len(name.encode("utf-8")) + len(value.encode("utf-8")) + 4
    except (AttributeError, UnicodeError, ValueError):
        return _CallbackOutcome.invalid()
    if (
        method != "GET"
        or header_bytes > _MAX_HEADER_BYTES
        or body
        or normalized_headers.get("host") != expected_authority
        or "transfer-encoding" in normalized_headers
        or normalized_headers.get("content-length", "0") != "0"
    ):
        return _CallbackOutcome.invalid()
    return _parse_callback_target(
        target,
        redirect_uri=redirect_uri,
        expected_state=expected_state,
    )


class _CallbackHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, _format: str, *args: object) -> None:
        return None

    def do_GET(self) -> None:
        server = cast(_CallbackHTTPServerState, self.server)
        server._outcome = _parse_callback_request(
            method=self.command,
            target=self.path,
            headers=cast(Mapping[str, str], self.headers),
            body=b"",
            redirect_uri=server._redirect_uri,
            expected_state=server._expected_state,
        )
        self._respond()

    def do_POST(self) -> None:
        self._reject_non_get()

    def do_PUT(self) -> None:
        self._reject_non_get()

    def do_PATCH(self) -> None:
        self._reject_non_get()

    def _reject_non_get(self) -> None:
        server = cast(_CallbackHTTPServerState, self.server)
        server._outcome = _CallbackOutcome.invalid()
        self._respond()

    def _respond(self) -> None:
        self.send_response(200)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(_CALLBACK_PAGE)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(_CALLBACK_PAGE)


class _CallbackHTTPServerState(Protocol):
    _expected_state: str
    _redirect_uri: str
    _outcome: _CallbackOutcome | None


def _new_callback_server(port: int, *, expected_state: str) -> _CallbackServer:
    server = HTTPServer(("127.0.0.1", port), _CallbackHandler)
    assigned_port = int(server.server_address[1])
    stateful = cast(_CallbackHTTPServerState, server)
    stateful._expected_state = expected_state
    stateful._redirect_uri = f"http://127.0.0.1:{assigned_port}/callback"
    stateful._outcome = None
    return cast(_CallbackServer, server)


def _serve_one(server: _CallbackServer) -> _CallbackOutcome:
    server.timeout = _CALLBACK_DEADLINE_SECONDS
    outcome = _CallbackOutcome.invalid()
    closed = False
    try:
        server.handle_request()
        outcome = server._outcome or _CallbackOutcome.timeout()
    except Exception:
        outcome = _CallbackOutcome.invalid()
    finally:
        closed = _close_callback_server(server)
    return outcome if closed else _CallbackOutcome.invalid()


def _close_callback_server(server: _CallbackServer) -> bool:
    try:
        server.server_close()
    except Exception:
        return False
    return True


def _read_manual_callback(
    reader: Callable[[], str], *, redirect_uri: str, expected_state: str
) -> _CallbackOutcome:
    try:
        target = reader()
    except Exception:
        return _CallbackOutcome.invalid()
    return _parse_callback_target(
        target,
        redirect_uri=redirect_uri,
        expected_state=expected_state,
        require_absolute=True,
    )


__all__ = []
