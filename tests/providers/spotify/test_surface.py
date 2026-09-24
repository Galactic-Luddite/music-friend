from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from music_friend.providers import spotify

PACKAGE = Path(__file__).parents[3] / "src" / "music_friend" / "providers" / "spotify"
PACKAGE_FILES = frozenset(
    {
        "__init__.py",
        "callback.py",
        "config.py",
        "credentials.py",
        "normalize.py",
        "oauth.py",
        "scopes.py",
        "source.py",
        "tokens.py",
        "transport.py",
    }
)

EXPECTED_IMPORTS = {
    "__init__.py": (
        "from music_friend.providers.spotify.config import SpotifySettings",
        "from music_friend.providers.spotify.credentials import CredentialStatus",
        (
            "from music_friend.providers.spotify.oauth import AuthorizationMode, "
            "AuthorizationResult, SpotifyAuthorization"
        ),
        "from music_friend.providers.spotify.source import SpotifySource",
        "from music_friend.providers.spotify.tokens import SpotifyTokenManager",
    ),
    "callback.py": (
        "from __future__ import annotations",
        "from collections.abc import Callable, Mapping",
        "from dataclasses import dataclass",
        "from enum import Enum",
        "from http.server import BaseHTTPRequestHandler, HTTPServer",
        "from typing import Protocol, cast",
        "from urllib.parse import parse_qsl, urlsplit",
        "import re",
    ),
    "config.py": (
        "from __future__ import annotations",
        "from collections.abc import Mapping",
        "from dataclasses import dataclass",
        "import re",
    ),
    "credentials.py": (
        "from __future__ import annotations",
        "from dataclasses import dataclass",
        "import json",
        "import re",
    ),
    "normalize.py": (
        "from __future__ import annotations",
        "from datetime import date, datetime",
        "from hashlib import sha256",
        (
            "from music_friend.domain import Artist, CatalogItem, CatalogItemBatch, "
            "IdentityConfidence, Release, ReleaseDatePrecision, SourceReference"
        ),
        "from music_friend.domain.text import sanitize_display_name",
        "from music_friend.errors import InvalidSourceResponseError",
        "import re",
    ),
    "oauth.py": (
        "from __future__ import annotations",
        "from collections.abc import Callable",
        "from dataclasses import dataclass",
        "from enum import Enum",
        "from music_friend.providers import Capability",
        (
            "from music_friend.providers.spotify.callback import _CallbackServer, "
            "_CallbackStatus, _close_callback_server, _new_callback_server, "
            "_read_manual_callback, _serve_one"
        ),
        "from music_friend.providers.spotify.config import SpotifySettings",
        "from music_friend.providers.spotify.scopes import _scopes_for_capabilities",
        "from music_friend.providers.spotify.tokens import SpotifyTokenManager",
        "from urllib.parse import urlencode",
        "import base64",
        "import hashlib",
        "import re",
        "import secrets",
    ),
    "scopes.py": (
        "from __future__ import annotations",
        "from collections.abc import Mapping",
        "from music_friend.providers import Capability, ProviderCapabilities",
        "from types import MappingProxyType",
    ),
    "source.py": (
        "from __future__ import annotations",
        "from collections.abc import Callable, Mapping, Sequence",
        "from datetime import datetime",
        (
            "from music_friend.domain import Artist, CatalogItem, CatalogItemBatch, Release, "
            "ReleaseDatePrecision, SourceReference"
        ),
        "from music_friend.errors import InvalidSourceResponseError",
        (
            "from music_friend.providers import Capability, HealthStatus, Page, "
            "ProviderCapabilities, ProviderHealth, require_capability"
        ),
        "from music_friend.providers.spotify.config import SpotifySettings",
        (
            "from music_friend.providers.spotify.normalize import normalize_artist, "
            "normalize_release, normalize_track_batch"
        ),
        "from music_friend.providers.spotify.transport import SpotifyOperation",
        "from typing import Protocol",
        "import base64",
        "import calendar",
        "import json",
        "import re",
    ),
    "tokens.py": (
        "from __future__ import annotations",
        "from collections.abc import Callable, Mapping",
        "from music_friend.errors import AuthenticationRequiredError, InvalidSourceResponseError",
        "from music_friend.providers import ProviderCapabilities",
        (
            "from music_friend.providers.credentials import CredentialKey, CredentialStore, "
            "CredentialStoreError"
        ),
        "from music_friend.providers.spotify.config import SpotifySettings",
        (
            "from music_friend.providers.spotify.credentials import CredentialStatus, "
            "_decode_credential, _encode_credential, _SpotifyCredential"
        ),
        "from music_friend.providers.spotify.scopes import _capabilities_for_scopes",
        ("from music_friend.providers.spotify.transport import SpotifyOperation, SpotifyTransport"),
        "import math",
        "import time",
        "from typing import Protocol, cast",
    ),
    "transport.py": (
        "from __future__ import annotations",
        "from collections.abc import Callable, Mapping, Sequence",
        "from dataclasses import dataclass",
        "from enum import Enum",
        (
            "from music_friend.errors import AdditionalScopeRequiredError, "
            "AuthenticationRequiredError, InvalidSourceResponseError, "
            "QuotaExhaustedError, RateLimitedError, SourceUnavailableError"
        ),
        "from types import MappingProxyType",
        "from typing import TypeAlias",
        "import httpx",
        "import json",
        "import math",
        "import re",
        "import time",
    ),
}

EXPECTED_EXPORTS = {
    "__init__.py": (
        "AuthorizationMode",
        "AuthorizationResult",
        "CredentialStatus",
        "SpotifyAuthorization",
        "SpotifySettings",
        "SpotifySource",
        "SpotifyTokenManager",
    ),
    "callback.py": (),
    "config.py": ("SpotifySettings", "load_spotify_settings"),
    "credentials.py": ("CredentialStatus",),
    "normalize.py": (
        "normalize_artist",
        "normalize_release",
        "normalize_track",
        "normalize_track_batch",
    ),
    "oauth.py": ("AuthorizationMode", "AuthorizationResult", "SpotifyAuthorization"),
    "scopes.py": (),
    "source.py": ("SpotifySource",),
    "tokens.py": ("SpotifyTokenManager",),
    "transport.py": ("SpotifyOperation", "SpotifyTransport"),
}

EXPECTED_PUBLIC_CLASSES = {
    "__init__.py": (),
    "callback.py": (),
    "config.py": ("SpotifySettings",),
    "credentials.py": ("CredentialStatus",),
    "normalize.py": (),
    "oauth.py": ("AuthorizationMode", "AuthorizationResult", "SpotifyAuthorization"),
    "scopes.py": (),
    "source.py": ("SpotifySource",),
    "tokens.py": ("SpotifyTokenManager",),
    "transport.py": ("SpotifyOperation", "SpotifyTransport"),
}

EXPECTED_PUBLIC_CALLABLES = {
    "__init__.py": (),
    "callback.py": (),
    "config.py": (("load_spotify_settings", "values: Mapping[str, object]", "SpotifySettings"),),
    "credentials.py": (),
    "normalize.py": (
        ("normalize_artist", "payload: object, *, observed_at: datetime", "Artist"),
        ("normalize_release", "payload: object, *, observed_at: datetime", "Release"),
        ("normalize_track", "payload: object, *, observed_at: datetime", "CatalogItem"),
        (
            "normalize_track_batch",
            "payload: object, *, observed_at: datetime",
            "CatalogItemBatch",
        ),
    ),
    "oauth.py": (
        (
            "SpotifyAuthorization.__init__",
            (
                "self, *, settings: SpotifySettings, tokens: SpotifyTokenManager, "
                "browser_opener: Callable[[str], bool], "
                "random_bytes: Callable[[int], bytes]=secrets.token_bytes, "
                "_server_factory: Callable[..., _CallbackServer]=_new_callback_server"
            ),
            "None",
        ),
        (
            "SpotifyAuthorization.authorize",
            (
                "self, capabilities: set[Capability] | frozenset[Capability], *, "
                "mode: AuthorizationMode, "
                "callback_reader: Callable[[], str] | None=None"
            ),
            "AuthorizationResult",
        ),
    ),
    "scopes.py": (),
    "source.py": (
        (
            "SpotifySource.__init__",
            (
                "self, *, settings: SpotifySettings, tokens: _TokenProvider, "
                "clock: Callable[[], datetime]"
            ),
            "None",
        ),
        ("SpotifySource.capabilities", "self", "ProviderCapabilities"),
        ("SpotifySource.followed_artists", "self, cursor: str | None=None", "Page[Artist]"),
        ("SpotifySource.health", "self", "ProviderHealth"),
        (
            "SpotifySource.recent_releases",
            "self, artist_refs: Sequence[SourceReference], since: datetime, cursor: str | None=None",
            "Page[Release]",
        ),
        (
            "SpotifySource.saved_items",
            "self, cursor: str | None=None",
            "CatalogItemBatch",
        ),
        ("SpotifySource.search_artists", "self, query: str, limit: int", "Page[Artist]"),
        (
            "SpotifySource.top_items",
            "self, time_range: str, limit: int",
            "CatalogItemBatch",
        ),
        (
            "SpotifySource.top_artists",
            "self, time_range: str, limit: int",
            "Page[Artist]",
        ),
    ),
    "tokens.py": (
        (
            "SpotifyTokenManager.__init__",
            (
                "self, *, settings: SpotifySettings, transport: SpotifyTransport, "
                "store: CredentialStore, clock: Callable[[], float]=time.monotonic"
            ),
            "None",
        ),
        ("SpotifyTokenManager.capabilities", "self", "ProviderCapabilities"),
        ("SpotifyTokenManager.disconnect", "self", "None"),
        ("SpotifyTokenManager.granted_scopes", "self", "tuple[str, ...]"),
        ("SpotifyTokenManager.status", "self", "CredentialStatus"),
    ),
    "transport.py": (
        (
            "SpotifyTransport.__init__",
            "self, connector: httpx.BaseTransport, *, clock: Callable[[], float]=time.monotonic",
            "None",
        ),
        ("SpotifyTransport.close", "self", "None"),
        (
            "SpotifyTransport.execute",
            (
                "self, operation: SpotifyOperation, *, query: ParameterPairs=(), "
                "form: ParameterPairs=(), access_token: str | None=None, "
                "deadline: float | None=None"
            ),
            "_TransportResponse",
        ),
        ("SpotifyTransport.trace", "self", "tuple[_TraceEntry, ...]"),
    ),
}

EXPECTED_RETURN_ROOTS = {
    "callback.py": {},
    "config.py": {"load_spotify_settings": ("SpotifySettings",)},
    "credentials.py": {},
    "normalize.py": {
        "normalize_artist": ("Artist",),
        "normalize_release": ("Release",),
        "normalize_track": ("CatalogItem",),
        "normalize_track_batch": ("CatalogItemBatch",),
    },
    "oauth.py": {
        "SpotifyAuthorization.__init__": (),
        "SpotifyAuthorization.authorize": ("AuthorizationResult", "failure"),
    },
    "scopes.py": {},
    "source.py": {
        "SpotifySource.__init__": (),
        "SpotifySource.capabilities": ("self._capabilities",),
        "SpotifySource.followed_artists": ("Page",),
        "SpotifySource.health": ("ProviderHealth",),
        "SpotifySource.recent_releases": ("Page",),
        "SpotifySource.saved_items": ("CatalogItemBatch",),
        "SpotifySource.search_artists": ("Page",),
        "SpotifySource.top_items": ("CatalogItemBatch",),
        "SpotifySource.top_artists": ("Page",),
    },
    "tokens.py": {
        "SpotifyTokenManager.__init__": (),
        "SpotifyTokenManager.capabilities": ("self._capabilities",),
        "SpotifyTokenManager.disconnect": (),
        "SpotifyTokenManager.granted_scopes": ("Tuple", "tuple"),
        "SpotifyTokenManager.status": ("CredentialStatus",),
    },
    "transport.py": {
        "SpotifyTransport.__init__": (),
        "SpotifyTransport.close": (),
        "SpotifyTransport.execute": ("_map_response",),
        "SpotifyTransport.trace": ("tuple",),
    },
}

EXPECTED_PUBLIC_ASSIGNMENTS = {
    "__init__.py": (),
    "callback.py": (),
    "config.py": (),
    "credentials.py": (),
    "normalize.py": (),
    "oauth.py": (),
    "scopes.py": (),
    "source.py": (),
    "tokens.py": (),
    "transport.py": ("JsonValue", "ParameterPairs"),
}

EXPECTED_PUBLIC_CLASS_ASSIGNMENTS = {
    "__init__.py": (),
    "callback.py": (),
    "config.py": ("SpotifySettings.client_id", "SpotifySettings.redirect_uri"),
    "credentials.py": (
        "CredentialStatus.connected",
        "CredentialStatus.granted_scopes",
    ),
    "normalize.py": (),
    "oauth.py": (
        "AuthorizationMode.DYNAMIC_LOOPBACK",
        "AuthorizationMode.FIXED_LOOPBACK",
        "AuthorizationMode.MANUAL",
        "AuthorizationResult.authorized",
        "AuthorizationResult.granted_capabilities",
    ),
    "scopes.py": (),
    "source.py": (),
    "tokens.py": (),
    "transport.py": (
        "SpotifyOperation.ARTIST_RELEASES",
        "SpotifyOperation.FOLLOWED_ARTISTS",
        "SpotifyOperation.HEALTH",
        "SpotifyOperation.SAVED_TRACKS",
        "SpotifyOperation.SEARCH_ARTISTS",
        "SpotifyOperation.TOKEN",
        "SpotifyOperation.TOP_ARTISTS",
        "SpotifyOperation.TOP_TRACKS",
    ),
}

EXPECTED_SETTINGS_FIELDS = (
    ("client_id", "str", "<required>"),
    ("redirect_uri", "str | None", "None"),
)
EXPECTED_ORIGINS = {
    "_ACCOUNTS_ORIGIN": "https://accounts.spotify.com",
    "_API_ORIGIN": "https://api.spotify.com",
}
EXPECTED_OPERATIONS = {
    "ARTIST_RELEASES": "artist_releases",
    "FOLLOWED_ARTISTS": "followed_artists",
    "HEALTH": "health",
    "SAVED_TRACKS": "saved_tracks",
    "SEARCH_ARTISTS": "search_artists",
    "TOKEN": "token",
    "TOP_TRACKS": "top_tracks",
    "TOP_ARTISTS": "top_artists",
}

TOKEN_FORM_KEYS = frozenset(
    {"client_id", "grant_type", "code", "redirect_uri", "code_verifier", "refresh_token"}
)
EXPECTED_REQUESTS = {
    "ARTIST_RELEASES": (
        "GET",
        "_API_ORIGIN",
        "/v1/artists/{artist_id}/albums",
        ("query_keys",),
        frozenset({"artist_id", "include_groups", "limit", "offset"}),
        frozenset(),
        (),
    ),
    "FOLLOWED_ARTISTS": (
        "GET",
        "_API_ORIGIN",
        "/v1/me/following",
        ("fixed_query", "query_keys"),
        frozenset({"limit", "after"}),
        frozenset(),
        (("type", "artist"),),
    ),
    "HEALTH": ("GET", "_API_ORIGIN", "/v1/me", (), frozenset(), frozenset(), ()),
    "SAVED_TRACKS": (
        "GET",
        "_API_ORIGIN",
        "/v1/me/tracks",
        ("query_keys",),
        frozenset({"limit", "offset"}),
        frozenset(),
        (),
    ),
    "SEARCH_ARTISTS": (
        "GET",
        "_API_ORIGIN",
        "/v1/search",
        ("fixed_query", "query_keys"),
        frozenset({"q", "limit"}),
        frozenset(),
        (("type", "artist"),),
    ),
    "TOKEN": (
        "POST",
        "_ACCOUNTS_ORIGIN",
        "/api/token",
        ("form_keys",),
        frozenset(),
        TOKEN_FORM_KEYS,
        (),
    ),
    "TOP_TRACKS": (
        "GET",
        "_API_ORIGIN",
        "/v1/me/top/tracks",
        ("query_keys",),
        frozenset({"time_range", "limit"}),
        frozenset(),
        (),
    ),
    "TOP_ARTISTS": (
        "GET",
        "_API_ORIGIN",
        "/v1/me/top/artists",
        ("query_keys",),
        frozenset({"time_range", "limit"}),
        frozenset(),
        (),
    ),
}

WRITE_METHODS = frozenset({"DELETE", "PATCH", "POST", "PUT"})
LISTENER_CALLS = frozenset(
    {
        "HTTPServer",
        "TCPServer",
        "ThreadingTCPServer",
        "bind",
        "create_server",
        "listen",
        "run_app",
        "serve_forever",
        "start_server",
    }
)
RAW_RETURN_PARTS = frozenset({"body", "json", "native", "payload", "raw", "response", "result"})
PROHIBITED_IDENTIFIER_PARTS = frozenset({"cover_art", "playback", "playlist"})
MUTATION_VERBS = frozenset({"add", "delete", "follow", "remove", "save", "unfollow", "update"})
SHELL_CALLS = frozenset({"call", "check_call", "check_output", "popen", "run", "system"})

FunctionNode = ast.FunctionDef | ast.AsyncFunctionDef


def _tokens(identifier: str) -> tuple[str, ...]:
    expanded = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", identifier).lower()
    return tuple(part for part in expanded.split("_") if part)


def _normalized(identifier: str) -> str:
    return "_".join(_tokens(identifier))


def _package_sources() -> dict[str, str]:
    return {path.name: path.read_text(encoding="utf-8") for path in PACKAGE.glob("*.py")}


def _parents(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    return {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}


def _enclosing_function(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> FunctionNode | None:
    current = parents.get(node)
    while current is not None:
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return current
        current = parents.get(current)
    return None


def _qualified_function_name(node: FunctionNode, parents: dict[ast.AST, ast.AST]) -> str:
    parent = parents.get(node)
    return f"{parent.name}.{node.name}" if isinstance(parent, ast.ClassDef) else node.name


def _call_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        owner = _call_name(node.value)
        return f"{owner}.{node.attr}" if owner else node.attr
    if isinstance(node, ast.Call):
        return _call_name(node.func)
    return ""


def _return_root(node: ast.expr) -> str:
    if isinstance(node, ast.Call):
        return _call_name(node.func)
    if isinstance(node, (ast.Name, ast.Attribute)):
        return _call_name(node)
    return type(node).__name__


def _imports(tree: ast.Module) -> tuple[str, ...]:
    return tuple(
        sorted(
            ast.unparse(node)
            for node in tree.body
            if isinstance(node, (ast.Import, ast.ImportFrom))
        )
    )


def _exports(tree: ast.Module) -> tuple[str, ...] | None:
    matches = [
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "__all__" for target in node.targets)
    ]
    if len(matches) != 1:
        return None
    try:
        value = ast.literal_eval(matches[0].value)
    except (ValueError, TypeError):
        return None
    return (
        tuple(value)
        if isinstance(value, list) and all(isinstance(item, str) for item in value)
        else None
    )


def _public_surface(
    tree: ast.Module,
) -> tuple[tuple[str, ...], tuple[tuple[str, str, str], ...], dict[str, FunctionNode]]:
    classes: list[str] = []
    callables: list[tuple[str, str, str]] = []
    nodes: dict[str, FunctionNode] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith(
            "_"
        ):
            returns = ast.unparse(node.returns) if node.returns is not None else "<missing>"
            surface_name = (
                f"async {node.name}" if isinstance(node, ast.AsyncFunctionDef) else node.name
            )
            callables.append((surface_name, ast.unparse(node.args), returns))
            nodes[node.name] = node
        elif isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
            classes.append(node.name)
            for method in node.body:
                if not isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)) or (
                    method.name.startswith("_") and method.name != "__init__"
                ):
                    continue
                qualified = f"{node.name}.{method.name}"
                returns = ast.unparse(method.returns) if method.returns is not None else "<missing>"
                surface_name = (
                    f"async {qualified}" if isinstance(method, ast.AsyncFunctionDef) else qualified
                )
                callables.append((surface_name, ast.unparse(method.args), returns))
                nodes[qualified] = method
    return tuple(sorted(classes)), tuple(sorted(callables)), nodes


def _assignment_targets(node: ast.AST) -> tuple[ast.expr, ...]:
    if isinstance(node, ast.Assign):
        return tuple(node.targets)
    if isinstance(node, (ast.AnnAssign, ast.AugAssign, ast.NamedExpr)):
        return (node.target,)
    return ()


def _assignment_target_leaves(target: ast.expr) -> tuple[ast.expr, ...]:
    if isinstance(target, (ast.List, ast.Tuple)):
        return tuple(leaf for element in target.elts for leaf in _assignment_target_leaves(element))
    if isinstance(target, ast.Starred):
        return _assignment_target_leaves(target.value)
    return (target,)


def _all_assignment_target_leaves(tree: ast.Module) -> tuple[ast.expr, ...]:
    return tuple(
        leaf
        for node in ast.walk(tree)
        for target in _assignment_targets(node)
        for leaf in _assignment_target_leaves(target)
    )


def _enclosing_assignment_scope(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> ast.AST | None:
    current = parents.get(node)
    while current is not None:
        if isinstance(
            current,
            (
                ast.Module,
                ast.ClassDef,
                ast.FunctionDef,
                ast.AsyncFunctionDef,
                ast.Lambda,
                ast.ListComp,
                ast.SetComp,
                ast.DictComp,
                ast.GeneratorExp,
            ),
        ):
            return current
        current = parents.get(current)
    return None


def _literal_name_aliases(tree: ast.Module, root: str) -> set[str]:
    aliases = {root}
    assignments = tuple(
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr))
    )
    changed = True
    while changed:
        changed = False
        for assignment in assignments:
            value = assignment.value
            if not isinstance(value, ast.Name) or value.id not in aliases:
                continue
            for target in _assignment_targets(assignment):
                if isinstance(target, ast.Name) and target.id not in aliases:
                    aliases.add(target.id)
                    changed = True
    return aliases


def _public_assignments(tree: ast.Module) -> tuple[str, ...]:
    names: set[str] = set()
    for node in tree.body:
        targets = (
            tuple(node.targets)
            if isinstance(node, ast.Assign)
            else ((node.target,) if isinstance(node, ast.AnnAssign) else ())
        )
        for target in targets:
            if isinstance(target, ast.Name) and not target.id.startswith("_"):
                names.add(target.id)
    names.discard("__all__")
    return tuple(sorted(names))


def _public_class_assignments(tree: ast.Module) -> tuple[str, ...]:
    public_class_nodes = {
        node: node.name
        for node in tree.body
        if isinstance(node, ast.ClassDef) and not node.name.startswith("_")
    }
    public_classes = set(public_class_nodes.values())
    setattr_aliases = _literal_name_aliases(tree, "setattr")
    parents = _parents(tree)
    names: set[str] = set()
    for target in _all_assignment_target_leaves(tree):
        if isinstance(target, ast.Name):
            scope = _enclosing_assignment_scope(target, parents)
            if scope not in public_class_nodes or target.id.startswith("_"):
                continue
            names.add(f"{public_class_nodes[scope]}.{target.id}")
        elif (
            isinstance(target, ast.Attribute)
            and isinstance(target.value, ast.Name)
            and target.value.id in public_classes
            and not target.attr.startswith("_")
        ):
            names.add(f"{target.value.id}.{target.attr}")
    for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
        if (
            not isinstance(call.func, ast.Name)
            or call.func.id not in setattr_aliases
            or len(call.args) != 3
            or call.keywords
            or not isinstance(call.args[0], ast.Name)
            or call.args[0].id not in public_classes
            or not isinstance(call.args[1], ast.Constant)
            or not isinstance(call.args[1].value, str)
            or call.args[1].value.startswith("_")
        ):
            continue
        names.add(f"{call.args[0].id}.{call.args[1].value}")
    return tuple(sorted(names))


def _settings_fields(tree: ast.Module) -> tuple[tuple[str, str, str], ...] | None:
    classes = [
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "SpotifySettings"
    ]
    if len(classes) != 1:
        return None
    fields = []
    for node in classes[0].body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            fields.append(
                (
                    node.target.id,
                    ast.unparse(node.annotation),
                    "<required>" if node.value is None else ast.unparse(node.value),
                )
            )
    return tuple(fields)


def _origin_constants(tree: ast.Module) -> dict[str, str] | None:
    origins: dict[str, str] = {}
    for node in tree.body:
        if (
            not isinstance(node, ast.Assign)
            or len(node.targets) != 1
            or not isinstance(node.targets[0], ast.Name)
        ):
            continue
        target = node.targets[0]
        if "origin" not in _tokens(target.id):
            continue
        if not isinstance(node.value, ast.Constant) or not isinstance(node.value.value, str):
            return None
        origins[target.id] = node.value.value
    return origins


def _operation_members(tree: ast.Module) -> dict[str, str] | None:
    classes = [
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "SpotifyOperation"
    ]
    if len(classes) != 1:
        return None
    members: dict[str, str] = {}
    for node in classes[0].body:
        if (
            not isinstance(node, ast.Assign)
            or len(node.targets) != 1
            or not isinstance(node.targets[0], ast.Name)
        ):
            continue
        if not isinstance(node.value, ast.Constant) or not isinstance(node.value.value, str):
            return None
        members[node.targets[0].id] = node.value.value
    return members


def _string_set(node: ast.expr) -> frozenset[str] | None:
    if (
        not isinstance(node, ast.Call)
        or not isinstance(node.func, ast.Name)
        or node.func.id != "frozenset"
        or len(node.args) != 1
        or node.keywords
    ):
        return None
    try:
        value = ast.literal_eval(node.args[0])
    except (ValueError, TypeError):
        return None
    return (
        frozenset(value)
        if isinstance(value, set) and all(isinstance(item, str) for item in value)
        else None
    )


def _fixed_query(node: ast.expr) -> tuple[tuple[str, str], ...] | None:
    try:
        value = ast.literal_eval(node)
    except (ValueError, TypeError):
        return None
    if not isinstance(value, tuple) or not all(
        isinstance(pair, tuple) and len(pair) == 2 and all(isinstance(item, str) for item in pair)
        for pair in value
    ):
        return None
    return value


def _request_spec(call: ast.Call) -> tuple[object, ...] | None:
    if (
        not isinstance(call.func, ast.Name)
        or call.func.id != "_RequestDefinition"
        or len(call.args) != 3
    ):
        return None
    method, origin, path = call.args
    if (
        not isinstance(method, ast.Constant)
        or not isinstance(method.value, str)
        or not isinstance(origin, ast.Name)
        or not isinstance(path, ast.Constant)
        or not isinstance(path.value, str)
    ):
        return None
    keyword_values: dict[str, ast.expr] = {}
    for keyword in call.keywords:
        if keyword.arg is None or keyword.arg in keyword_values:
            return None
        keyword_values[keyword.arg] = keyword.value
    if not set(keyword_values) <= {"query_keys", "form_keys", "fixed_query"}:
        return None
    query_keys = (
        _string_set(keyword_values["query_keys"]) if "query_keys" in keyword_values else frozenset()
    )
    form_keys = (
        _string_set(keyword_values["form_keys"]) if "form_keys" in keyword_values else frozenset()
    )
    fixed_query = (
        _fixed_query(keyword_values["fixed_query"]) if "fixed_query" in keyword_values else ()
    )
    if query_keys is None or form_keys is None or fixed_query is None:
        return None
    return (
        method.value,
        origin.id,
        path.value,
        tuple(sorted(keyword_values)),
        query_keys,
        form_keys,
        fixed_query,
    )


def _request_table(tree: ast.Module) -> dict[str, tuple[object, ...]] | None:
    matches = [
        node
        for node in tree.body
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id == "_REQUESTS"
    ]
    if len(matches) != 1:
        return None
    value = matches[0].value
    if (
        not isinstance(value, ast.Call)
        or not isinstance(value.func, ast.Name)
        or value.func.id != "MappingProxyType"
        or len(value.args) != 1
        or value.keywords
        or not isinstance(value.args[0], ast.Dict)
    ):
        return None
    requests: dict[str, tuple[object, ...]] = {}
    for key, definition in zip(value.args[0].keys, value.args[0].values, strict=True):
        if (
            not isinstance(key, ast.Attribute)
            or not isinstance(key.value, ast.Name)
            or key.value.id != "SpotifyOperation"
            or not isinstance(definition, ast.Call)
            or key.attr in requests
        ):
            return None
        spec = _request_spec(definition)
        if spec is None:
            return None
        requests[key.attr] = spec
    return requests


def _direct_returns(
    function: FunctionNode, parents: dict[ast.AST, ast.AST]
) -> tuple[ast.Return, ...]:
    return tuple(
        node
        for node in ast.walk(function)
        if isinstance(node, ast.Return) and _enclosing_function(node, parents) is function
    )


def _return_chain_parts(expression: ast.expr) -> set[str]:
    parts: set[str] = set()

    def visit(node: ast.expr) -> None:
        if isinstance(node, ast.Name):
            parts.update(_tokens(node.id))
        elif isinstance(node, ast.Attribute):
            visit(node.value)
            parts.update(_tokens(node.attr))
        elif isinstance(node, ast.Call):
            visit(node.func)
        elif isinstance(node, ast.Subscript):
            visit(node.value)
        else:
            for child in ast.iter_child_nodes(node):
                if isinstance(child, ast.expr):
                    visit(child)

    visit(expression)
    return parts


def _return_chain_names(expression: ast.expr) -> set[str]:
    names: set[str] = set()

    def visit(node: ast.expr) -> None:
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            visit(node.value)
        elif isinstance(node, ast.Call):
            visit(node.func)
        elif isinstance(node, ast.Subscript):
            visit(node.value)
        else:
            for child in ast.iter_child_nodes(node):
                if isinstance(child, ast.expr):
                    visit(child)

    visit(expression)
    return names


def _tainted_aliases(function: FunctionNode, parents: dict[ast.AST, ast.AST]) -> set[str]:
    assignments = [
        node
        for node in ast.walk(function)
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        and _enclosing_function(node, parents) is function
    ]
    tainted: set[str] = set()
    changed = True
    while changed:
        changed = False
        for assignment in assignments:
            value = assignment.value
            if value is None:
                continue
            value_names = {node.id for node in ast.walk(value) if isinstance(node, ast.Name)}
            value_parts = {
                part
                for node in ast.walk(value)
                if isinstance(node, (ast.Name, ast.Attribute))
                for part in _tokens(node.id if isinstance(node, ast.Name) else node.attr)
            }
            if not (value_parts & RAW_RETURN_PARTS or value_names & tainted):
                continue
            targets = (
                assignment.targets if isinstance(assignment, ast.Assign) else (assignment.target,)
            )
            for target in targets:
                if isinstance(target, ast.Name) and target.id not in tainted:
                    tainted.add(target.id)
                    changed = True
    return tainted


def _exact_response_boundary(filename: str, qualified: str, returned: ast.Return) -> bool:
    return (
        filename == "transport.py"
        and qualified == "SpotifyTransport.execute"
        and returned.value is not None
        and ast.unparse(returned.value) == "_map_response(status, headers, body)"
    )


def _exact_authorization_result(
    filename: str, qualified: str, function: FunctionNode, returned: ast.Return
) -> bool:
    if filename != "oauth.py" or qualified != "SpotifyAuthorization.authorize":
        return False
    initializers = [
        node
        for node in ast.walk(function)
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id == "failure"
    ]
    if len(initializers) != 1 or ast.unparse(initializers[0]) != (
        "failure = AuthorizationResult(False, frozenset())"
    ):
        return False
    return returned.value is not None and ast.unparse(returned.value) in {
        "failure",
        "AuthorizationResult(True, granted)",
    }


def _raw_return_violation(
    filename: str, qualified: str, function: FunctionNode, parents: dict[ast.AST, ast.AST]
) -> bool:
    expected_roots = EXPECTED_RETURN_ROOTS.get(filename, {}).get(qualified)
    if expected_roots is None:
        return True
    returned = _direct_returns(function, parents)
    actual_roots = tuple(
        sorted({_return_root(node.value) for node in returned if node.value is not None})
    )
    if actual_roots != expected_roots:
        return True
    tainted = _tainted_aliases(function, parents)
    for node in returned:
        if node.value is None or _exact_response_boundary(filename, qualified, node):
            continue
        if _exact_authorization_result(filename, qualified, function, node):
            continue
        if (
            _return_chain_parts(node.value) & RAW_RETURN_PARTS
            or _return_chain_names(node.value) & tainted
        ):
            return True
    return False


def _response_boundary_is_exact(tree: ast.Module, parents: dict[ast.AST, ast.AST]) -> bool:
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_map_response"
    ]
    if len(calls) != 1:
        return False
    call = calls[0]
    parent = parents.get(call)
    function = _enclosing_function(call, parents)
    if (
        not isinstance(parent, ast.Return)
        or function is None
        or _qualified_function_name(function, parents) != "SpotifyTransport.execute"
        or ast.unparse(call) != "_map_response(status, headers, body)"
    ):
        return False
    mappers = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_map_response"
    ]
    if (
        len(mappers) != 1
        or ast.unparse(mappers[0].args) != "status: int, headers: httpx.Headers, body: bytes"
        or ast.unparse(mappers[0].returns) != "_TransportResponse"
    ):
        return False
    roots = tuple(
        sorted(
            {
                _return_root(node.value)
                for node in _direct_returns(mappers[0], parents)
                if node.value is not None
            }
        )
    )
    return roots == ("_TransportResponse",)


def _listener_violation(filename: str, tree: ast.Module) -> bool:
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        for imported in node.names:
            if imported.name.rsplit(".", 1)[-1] in LISTENER_CALLS:
                aliases.add(imported.asname or imported.name)
    assignments = [node for node in ast.walk(tree) if isinstance(node, (ast.Assign, ast.AnnAssign))]
    changed = True
    while changed:
        changed = False
        for assignment in assignments:
            if assignment.value is None:
                continue
            name = _call_name(assignment.value)
            if name.rsplit(".", 1)[-1] not in LISTENER_CALLS and name not in aliases:
                continue
            targets = (
                assignment.targets if isinstance(assignment, ast.Assign) else (assignment.target,)
            )
            for target in targets:
                if isinstance(target, ast.Name) and target.id not in aliases:
                    aliases.add(target.id)
                    changed = True
    listener_calls = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node.func)
        if name.rsplit(".", 1)[-1] in LISTENER_CALLS or name in aliases:
            listener_calls.append(node)
    if filename != "callback.py":
        return bool(listener_calls)
    if len(listener_calls) != 1:
        return True
    call = listener_calls[0]
    parents = _parents(tree)
    function = _enclosing_function(call, parents)
    return not (
        function is not None
        and function.name == "_new_callback_server"
        and ast.unparse(call) == "HTTPServer(('127.0.0.1', port), _CallbackHandler)"
    )


def _prohibited_identifier_violation(tree: ast.Module) -> bool:
    for node in ast.walk(tree):
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and set(_tokens(node.name)) & MUTATION_VERBS
        ):
            return True
        if isinstance(node, ast.Name):
            normalized = _normalized(node.id)
        elif isinstance(node, ast.Attribute):
            normalized = _normalized(node.attr)
        else:
            continue
        if any(part in normalized for part in PROHIBITED_IDENTIFIER_PARTS):
            return True
    return False


def _shell_violation(tree: ast.Module) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            owner = _call_name(node.func.value).split(".", 1)[0]
            if owner in {"os", "subprocess"} and node.func.attr.lower() in SHELL_CALLS:
                return True
    return False


def _write_method_surface(files: dict[str, ast.Module]) -> tuple[tuple[str, str], ...]:
    return tuple(
        sorted(
            (filename, node.value.upper())
            for filename, tree in files.items()
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value.upper() in WRITE_METHODS
        )
    )


def _surface_violations(files: dict[str, str]) -> tuple[str, ...]:
    if set(files) != PACKAGE_FILES:
        return ("module-set",)
    try:
        trees = {
            filename: ast.parse(source, filename=filename) for filename, source in files.items()
        }
    except SyntaxError:
        return ("syntax",)
    violations: set[str] = set()
    callable_nodes: dict[str, dict[str, FunctionNode]] = {}
    parents_by_file = {filename: _parents(tree) for filename, tree in trees.items()}
    for filename, tree in trees.items():
        if _imports(tree) != tuple(sorted(EXPECTED_IMPORTS[filename])):
            violations.add("imports")
        if _exports(tree) != EXPECTED_EXPORTS[filename]:
            violations.add("exports")
        classes, callables, nodes = _public_surface(tree)
        callable_nodes[filename] = nodes
        if (
            classes != EXPECTED_PUBLIC_CLASSES[filename]
            or callables != tuple(sorted(EXPECTED_PUBLIC_CALLABLES[filename]))
            or _public_assignments(tree) != EXPECTED_PUBLIC_ASSIGNMENTS[filename]
            or _public_class_assignments(tree) != EXPECTED_PUBLIC_CLASS_ASSIGNMENTS[filename]
        ):
            violations.add("public-surface")
        if _listener_violation(filename, tree):
            violations.add("listener")
        if _prohibited_identifier_violation(tree):
            violations.add("prohibited-symbol")
        if _shell_violation(tree):
            violations.add("shell")
    if _settings_fields(trees["config.py"]) != EXPECTED_SETTINGS_FIELDS:
        violations.add("public-surface")
    if _origin_constants(trees["transport.py"]) != EXPECTED_ORIGINS:
        violations.add("origins")
    if (
        _operation_members(trees["transport.py"]) != EXPECTED_OPERATIONS
        or _request_table(trees["transport.py"]) != EXPECTED_REQUESTS
    ):
        violations.add("request-table")
    if _write_method_surface(trees) != (("transport.py", "POST"),):
        violations.add("write-method")
    if not _response_boundary_is_exact(trees["transport.py"], parents_by_file["transport.py"]):
        violations.add("raw-return")
    for filename, nodes in callable_nodes.items():
        for qualified, function in nodes.items():
            if _raw_return_violation(filename, qualified, function, parents_by_file[filename]):
                violations.add("raw-return")
    return tuple(sorted(violations))


def _mutated_package(filename: str, old: str, new: str) -> dict[str, str]:
    files = _package_sources()
    assert old in files[filename]
    files[filename] = files[filename].replace(old, new, 1)
    return files


TOKEN_FORM_BLOCK = """            form_keys=frozenset(
                {
                    "client_id",
                    "grant_type",
                    "code",
                    "redirect_uri",
                    "code_verifier",
                    "refresh_token",
                }
            ),
"""


def test_public_package_exports_only_safe_high_level_interfaces() -> None:
    assert spotify.__all__ == [
        "AuthorizationMode",
        "AuthorizationResult",
        "CredentialStatus",
        "SpotifyAuthorization",
        "SpotifySettings",
        "SpotifySource",
        "SpotifyTokenManager",
    ]
    assert not hasattr(spotify, "SpotifyTransport")
    assert not hasattr(spotify, "SpotifyOperation")


def test_package_matches_the_closed_task_two_surface() -> None:
    assert _surface_violations(_package_sources()) == ()


def test_module_level_async_callable_cannot_widen_the_public_surface() -> None:
    files = _mutated_package(
        "source.py",
        "class SpotifySource:",
        "async def arbitrary_request(endpoint):\n    return endpoint\n\n\nclass SpotifySource:",
    )

    assert "public-surface" in _surface_violations(files)


def test_public_callable_sync_async_kind_is_exact() -> None:
    files = _mutated_package(
        "source.py",
        "    def capabilities(self) -> ProviderCapabilities:\n"
        '        """Return the precomputed local capability snapshot without I/O."""\n'
        "        return self._capabilities",
        "    async def capabilities(self) -> ProviderCapabilities:\n"
        '        """Return the precomputed local capability snapshot without I/O."""\n'
        "        return self._capabilities",
    )

    assert "public-surface" in _surface_violations(files)


def test_async_public_method_is_checked_for_raw_returns() -> None:
    files = _mutated_package(
        "source.py",
        "    def health(self) -> ProviderHealth:\n"
        "        deadline = self._call_deadline()\n"
        "        self._authorize(Capability.HEALTH)\n"
        "        self._execute(SpotifyOperation.HEALTH, deadline=deadline)\n"
        "        return ProviderHealth(HealthStatus.HEALTHY, self._capabilities)",
        "    async def health(self) -> ProviderHealth:\n        return self._transport",
    )

    assert "raw-return" in _surface_violations(files)


def test_private_async_mutation_callable_is_prohibited() -> None:
    files = _mutated_package(
        "source.py",
        "class SpotifySource:",
        "async def _save_item(item):\n    return item\n\n\nclass SpotifySource:",
    )

    assert "prohibited-symbol" in _surface_violations(files)


@pytest.mark.parametrize(
    "injection",
    (
        "    raw_response = lambda self: self._transport\n",
        "    raw_response: object = property(lambda self: self._transport)\n",
        "    @property\n"
        "    async def raw_response(self) -> object:\n"
        "        return self._transport\n",
    ),
    ids=("assignment", "descriptor-assignment", "async-property"),
)
def test_exported_class_rejects_non_allowlisted_public_members(injection: str) -> None:
    files = _mutated_package(
        "source.py",
        'class SpotifySource:\n    """Normalize the complete bounded Spotify read surface."""\n',
        'class SpotifySource:\n    """Normalize the complete bounded Spotify read surface."""\n\n'
        + injection,
    )

    assert "public-surface" in _surface_violations(files)


def test_exported_class_rejects_module_level_public_attribute_injection() -> None:
    files = _mutated_package(
        "source.py",
        '__all__ = ["SpotifySource"]',
        'SpotifySource.raw_response = lambda self: self._transport\n\n__all__ = ["SpotifySource"]',
    )

    assert "public-surface" in _surface_violations(files)


def test_exported_class_rejects_literal_setattr_public_attribute_injection() -> None:
    files = _mutated_package(
        "source.py",
        '__all__ = ["SpotifySource"]',
        'setattr(SpotifySource, "raw_response", lambda self: self._transport)\n\n'
        '__all__ = ["SpotifySource"]',
    )

    assert "public-surface" in _surface_violations(files)


def test_exported_class_rejects_named_expression_public_member() -> None:
    files = _mutated_package(
        "source.py",
        'class SpotifySource:\n    """Normalize the complete bounded Spotify read surface."""\n',
        'class SpotifySource:\n    """Normalize the complete bounded Spotify read surface."""\n\n'
        "    (raw_response := lambda self: self._transport)\n",
    )

    assert "public-surface" in _surface_violations(files)


@pytest.mark.parametrize(
    "injection",
    (
        "    (raw_response, _private) = (lambda self: self._transport, None)\n",
        "    [raw_response, _private] = [lambda self: self._transport, None]\n",
        "    (_private, [raw_response]) = (None, [lambda self: self._transport])\n",
        "    (*raw_response, _private) = (lambda self: self._transport, None)\n",
    ),
    ids=("tuple", "list", "nested", "starred"),
)
def test_exported_class_rejects_public_members_in_destructuring_targets(
    injection: str,
) -> None:
    files = _mutated_package(
        "source.py",
        'class SpotifySource:\n    """Normalize the complete bounded Spotify read surface."""\n',
        'class SpotifySource:\n    """Normalize the complete bounded Spotify read surface."""\n\n'
        + injection,
    )

    assert "public-surface" in _surface_violations(files)


def test_exported_class_rejects_literal_setattr_alias_injection() -> None:
    files = _mutated_package(
        "source.py",
        '__all__ = ["SpotifySource"]',
        '_set_class_member = setattr\n_set_class_member(SpotifySource, "raw_response", None)\n\n'
        '__all__ = ["SpotifySource"]',
    )

    assert "public-surface" in _surface_violations(files)


@pytest.mark.parametrize(
    "injection",
    (
        "    (_raw_response, *_private) = (None, None)\n",
        "    (_raw_response := None)\n",
    ),
    ids=("private-destructuring", "private-named-expression"),
)
def test_exported_class_allows_private_assignment_targets(injection: str) -> None:
    files = _mutated_package(
        "source.py",
        'class SpotifySource:\n    """Normalize the complete bounded Spotify read surface."""\n',
        'class SpotifySource:\n    """Normalize the complete bounded Spotify read surface."""\n\n'
        + injection,
    )

    assert _surface_violations(files) == ()


def test_exported_class_allows_literal_setattr_of_private_member() -> None:
    files = _mutated_package(
        "source.py",
        '__all__ = ["SpotifySource"]',
        '_set_class_member = setattr\n_set_class_member(SpotifySource, "_raw_response", None)\n\n'
        '__all__ = ["SpotifySource"]',
    )

    assert _surface_violations(files) == ()


def test_exported_class_gate_ignores_setattr_on_unrelated_private_class() -> None:
    files = _mutated_package(
        "source.py",
        '__all__ = ["SpotifySource"]',
        'class _LocalSource:\n    pass\n\nsetattr(_LocalSource, "raw_response", None)\n\n'
        '__all__ = ["SpotifySource"]',
    )

    assert _surface_violations(files) == ()


def test_exported_class_gate_ignores_unrelated_local_destructuring() -> None:
    files = _mutated_package(
        "source.py",
        "def _object(value: object, field: str) -> dict[str, object]:",
        "def _object(value: object, field: str) -> dict[str, object]:\n"
        "    raw_response, [*remaining] = (value, [])",
    )

    assert _surface_violations(files) == ()


@pytest.mark.parametrize(
    "replacement",
    (
        "",
        TOKEN_FORM_BLOCK.replace('"refresh_token",', '"refresh_token", "client_secret",'),
        TOKEN_FORM_BLOCK + "            query_keys=frozenset(),\n",
        TOKEN_FORM_BLOCK + '            query_keys=frozenset({"q"}),\n',
        TOKEN_FORM_BLOCK + "            fixed_query=(),\n",
        TOKEN_FORM_BLOCK + '            fixed_query=(("type", "artist"),),\n',
    ),
    ids=(
        "missing-form-keys",
        "extra-form-key",
        "empty-query-keys",
        "query-keys",
        "empty-fixed-query",
        "fixed-query",
    ),
)
def test_token_request_definition_requires_the_exact_parameter_shape(replacement: str) -> None:
    assert "request-table" in _surface_violations(
        _mutated_package("transport.py", TOKEN_FORM_BLOCK, replacement)
    )


@pytest.mark.parametrize(
    ("filename", "old", "new"),
    (
        (
            "config.py",
            "return SpotifySettings(client_id=client_id, redirect_uri=redirect_uri)",
            "return _map_response(status, headers, body)",
        ),
        (
            "source.py",
            "return ProviderHealth(HealthStatus.HEALTHY, self._capabilities)",
            "return _map_response(status, headers, body)",
        ),
        (
            "transport.py",
            "return _map_response(status, headers, body)",
            "mapped = body\n            return mapped",
        ),
        (
            "transport.py",
            "return _map_response(status, headers, body)",
            "mapped = body\n            alias = mapped\n            return alias",
        ),
    ),
    ids=("config-wrapper", "source-wrapper", "one-step-alias", "two-step-alias"),
)
def test_raw_response_mapping_is_allowed_only_at_the_exact_transport_boundary(
    filename: str, old: str, new: str
) -> None:
    assert "raw-return" in _surface_violations(_mutated_package(filename, old, new))


@pytest.mark.parametrize(
    ("old", "new"),
    (
        (
            "failure = AuthorizationResult(False, frozenset())",
            "failure = AuthorizationResult(False, frozenset({self._transport}))",
        ),
        (
            "return AuthorizationResult(True, granted)",
            "return AuthorizationResult(True, frozenset({response}))",
        ),
    ),
    ids=("failure-result", "success-result"),
)
def test_authorization_result_boundary_allows_only_redacted_values(old: str, new: str) -> None:
    assert "raw-return" in _surface_violations(_mutated_package("oauth.py", old, new))


@pytest.mark.parametrize(
    "listener",
    (
        "ThreadingTCPServer(address, handler)",
        "socketserver.ThreadingTCPServer(address, handler)",
        "web.run_app(application)",
        "factory = ThreadingTCPServer\n    factory(address, handler)",
    ),
    ids=("imported-threading-server", "qualified-threading-server", "web-run-app", "alias"),
)
def test_task_two_rejects_every_listener_constructor_and_runner(listener: str) -> None:
    old = "    return SpotifySettings(client_id=client_id, redirect_uri=redirect_uri)"
    files = _mutated_package("config.py", old, f"    {listener}\n{old}")
    assert "listener" in _surface_violations(files)


@pytest.mark.parametrize(
    ("injected_import", "listener"),
    (
        (
            "from socketserver import ThreadingTCPServer as SafeServer",
            "SafeServer(address, handler)",
        ),
        ("from aiohttp.web import run_app as launch", "launch(application)"),
    ),
    ids=("renamed-threading-server", "renamed-web-runner"),
)
def test_listener_gate_propagates_imported_aliases(injected_import: str, listener: str) -> None:
    files = _mutated_package(
        "config.py",
        "from dataclasses import dataclass",
        f"from dataclasses import dataclass\n{injected_import}",
    )
    old = "    return SpotifySettings(client_id=client_id, redirect_uri=redirect_uri)"
    files["config.py"] = files["config.py"].replace(old, f"    {listener}\n{old}", 1)

    assert "listener" in _surface_violations(files)


def test_callback_listener_allowance_is_module_scoped() -> None:
    files = _mutated_package(
        "config.py",
        "from dataclasses import dataclass",
        "from dataclasses import dataclass\nfrom http.server import HTTPServer",
    )
    old = "    return SpotifySettings(client_id=client_id, redirect_uri=redirect_uri)"
    files["config.py"] = files["config.py"].replace(
        old,
        '    HTTPServer(("127.0.0.1", port), _CallbackHandler)\n' + old,
        1,
    )

    assert "listener" in _surface_violations(files)


@pytest.mark.parametrize(
    "replacement",
    (
        'HTTPServer(("localhost", port), _CallbackHandler)',
        'HTTPServer(("0.0.0.0", port), _CallbackHandler)',
        'HTTPServer(("127.0.0.1", 0), _CallbackHandler)',
        'HTTPServer(("127.0.0.1", port), handler)',
    ),
    ids=("localhost", "all-interfaces", "fixed-zero", "caller-handler"),
)
def test_callback_listener_allowance_requires_the_exact_construction(
    replacement: str,
) -> None:
    files = _mutated_package(
        "callback.py",
        'HTTPServer(("127.0.0.1", port), _CallbackHandler)',
        replacement,
    )

    assert "listener" in _surface_violations(files)


def test_callback_listener_allowance_rejects_an_additional_listener() -> None:
    files = _mutated_package(
        "callback.py",
        '    server = HTTPServer(("127.0.0.1", port), _CallbackHandler)',
        '    extra = HTTPServer(("127.0.0.1", port), _CallbackHandler)\n'
        '    server = HTTPServer(("127.0.0.1", port), _CallbackHandler)',
    )

    assert "listener" in _surface_violations(files)


def test_callback_listener_allowance_rejects_an_alias_bypass() -> None:
    files = _mutated_package(
        "callback.py",
        '    server = HTTPServer(("127.0.0.1", port), _CallbackHandler)',
        '    listener = HTTPServer\n    server = listener(("127.0.0.1", port), _CallbackHandler)',
    )

    assert "listener" in _surface_violations(files)


def test_redirect_uri_is_allowed_only_on_the_validated_config_model() -> None:
    files = _mutated_package(
        "config.py",
        "def load_spotify_settings(values: Mapping[str, object]) -> SpotifySettings:",
        "def load_spotify_settings(\n    values: Mapping[str, object], redirect_uri: str\n) -> SpotifySettings:",
    )
    assert "public-surface" in _surface_violations(files)


@pytest.mark.parametrize(
    ("safe", "unsafe"),
    (
        ('"https://accounts.spotify.com"', '"https://attacker.invalid"'),
        ('"https://api.spotify.com"', '"https://attacker.invalid"'),
    ),
    ids=("accounts", "api"),
)
def test_fixed_origin_names_require_the_exact_literal_values(safe: str, unsafe: str) -> None:
    assert "origins" in _surface_violations(_mutated_package("transport.py", safe, unsafe))


@pytest.mark.parametrize(
    ("filename", "old", "new", "expected"),
    (
        (
            "config.py",
            "from dataclasses import dataclass",
            "from dataclasses import dataclass\nimport socketserver",
            "imports",
        ),
        (
            "config.py",
            "def load_spotify_settings(values: Mapping[str, object]) -> SpotifySettings:",
            "def load_spotify_settings(values: Mapping[str, object], uri: str) -> SpotifySettings:",
            "public-surface",
        ),
        (
            "source.py",
            "def _object(value: object, field: str) -> dict[str, object]:",
            "def save_playlist(value: object, field: str) -> dict[str, object]:",
            "prohibited-symbol",
        ),
        (
            "config.py",
            "return SpotifySettings(client_id=client_id, redirect_uri=redirect_uri)",
            'verb = "DELETE"\n    return SpotifySettings(client_id=client_id, redirect_uri=redirect_uri)',
            "write-method",
        ),
        (
            "config.py",
            "return SpotifySettings(client_id=client_id, redirect_uri=redirect_uri)",
            'os.system("command")\n    return SpotifySettings(client_id=client_id, redirect_uri=redirect_uri)',
            "shell",
        ),
    ),
    ids=("import", "uri-signature", "mutation", "write", "shell"),
)
def test_closed_surface_controls_are_individually_non_vacuous(
    filename: str, old: str, new: str, expected: str
) -> None:
    assert expected in _surface_violations(_mutated_package(filename, old, new))
