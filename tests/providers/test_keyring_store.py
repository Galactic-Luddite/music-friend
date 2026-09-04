from __future__ import annotations

import traceback
from types import SimpleNamespace

import pytest

from music_friend.providers import keyring_store
from music_friend.providers.credentials import CredentialKey, CredentialStoreError
from music_friend.providers.keyring_store import KeyringCredentialStore, _BackendFailure


class SecureMacBackend:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def set_password(self, service: str, account: str, value: str) -> None:
        self.values[(service, account)] = value

    def get_password(self, service: str, account: str) -> str | None:
        return self.values.get((service, account))

    def delete_password(self, service: str, account: str) -> None:
        self.values.pop((service, account), None)


class OtherBackend(SecureMacBackend):
    pass


class IncompleteBackend:
    def set_password(self, service: str, account: str, value: str) -> None:
        pass

    def get_password(self, service: str, account: str) -> str | None:
        return None


class NonTextBackend(SecureMacBackend):
    def get_password(self, service: str, account: str) -> str | None:
        return 1  # type: ignore[return-value]


class _RawBackendError(RuntimeError):
    def __init__(self, operation: str) -> None:
        super().__init__(f"backend-message-canary:{operation}")
        self.backend_details = {"backend-attribute-canary": operation}


class FailingBackend(SecureMacBackend):
    def __init__(self, operation: str) -> None:
        super().__init__()
        self.operation = operation

    def set_password(self, service: str, account: str, value: str) -> None:
        if self.operation == "save":
            raise _RawBackendError("save")
        super().set_password(service, account, value)

    def get_password(self, service: str, account: str) -> str | None:
        if self.operation == "load":
            raise _RawBackendError("load")
        return super().get_password(service, account)

    def delete_password(self, service: str, account: str) -> None:
        if self.operation == "delete":
            raise _RawBackendError("delete")
        super().delete_password(service, account)


def _factory(backend: object, approved: type[object] = SecureMacBackend):
    return lambda: (backend, approved)


def _exception_graph(error: BaseException) -> tuple[BaseException, ...]:
    pending = [error]
    seen: set[int] = set()
    result: list[BaseException] = []
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        result.append(current)
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        if current.__context__ is not None:
            pending.append(current.__context__)
    return tuple(result)


def _exception_graph_surface(error: BaseException) -> str:
    return "|".join(repr(node) + str(node) + repr(vars(node)) for node in _exception_graph(error))


def test_secure_exact_backend_round_trip_and_absent_delete() -> None:
    backend = SecureMacBackend()
    store = KeyringCredentialStore(_backend_factory=_factory(backend))
    key = CredentialKey("spotify", "client-a")

    assert store.load(key) is None
    store.delete(key)
    store.save(key, "credential-envelope")
    assert store.load(key) == "credential-envelope"
    store.delete(key)
    assert store.load(key) is None
    assert store.scheduled_eligible is True


@pytest.mark.parametrize(
    "backend",
    [
        OtherBackend(),
        object(),
        type("NullKeyring", (), {})(),
        type("FailKeyring", (), {})(),
        type("PlaintextKeyring", (), {})(),
        type("ChainerBackend", (), {})(),
    ],
)
def test_every_non_exact_backend_fails_closed(backend: object) -> None:
    with pytest.raises(CredentialStoreError) as raised:
        KeyringCredentialStore(_backend_factory=_factory(backend))

    assert str(raised.value) == "Credential storage is unavailable."


def _raise_backend_failure(operation: str) -> CredentialStoreError:
    if operation == "factory":

        def failing_factory() -> tuple[object, type[object]]:
            raise _RawBackendError("factory")

        with pytest.raises(CredentialStoreError) as raised:
            KeyringCredentialStore(_backend_factory=failing_factory)
        return raised.value

    backend = FailingBackend(operation)
    store = KeyringCredentialStore(_backend_factory=_factory(backend, FailingBackend))
    key = CredentialKey("spotify", "client-canary")
    if operation == "delete":
        backend.values[(key._service, "credential")] = "credential-envelope-canary"

    with pytest.raises(CredentialStoreError) as raised:
        if operation == "save":
            store.save(key, "credential-envelope-canary")
        elif operation == "load":
            store.load(key)
        else:
            store.delete(key)
    return raised.value


@pytest.mark.parametrize("operation", ["factory", "save", "load", "delete"])
def test_backend_failure_has_only_a_sanitized_private_cause(operation: str) -> None:
    error = _raise_backend_failure(operation)
    graph = _exception_graph(error)

    assert len(graph) == 2
    assert graph[0] is error
    assert error.__context__ is None
    safe_cause = graph[1]
    assert type(safe_cause) is _BackendFailure
    assert error.__cause__ is safe_cause
    assert safe_cause.args == ()
    assert vars(safe_cause) == {}
    assert safe_cause.__cause__ is None
    assert safe_cause.__context__ is None

    rendered = "".join(traceback.format_exception(error))
    assert "backend-message-canary" not in rendered
    assert "client-canary" not in rendered
    surface = _exception_graph_surface(error)
    assert "backend-message-canary" not in surface
    assert "backend-attribute-canary" not in surface
    assert "credential-envelope-canary" not in surface


def test_exception_graph_recorder_detects_a_deliberate_backend_canary() -> None:
    try:
        raise _RawBackendError("deliberate")
    except _RawBackendError as backend_error:
        try:
            raise _BackendFailure() from backend_error
        except _BackendFailure as unsafe_private_failure:
            try:
                raise CredentialStoreError() from unsafe_private_failure
            except CredentialStoreError as public_error:
                recorded = _exception_graph_surface(public_error)

    assert "backend-message-canary:deliberate" in recorded
    assert "backend-attribute-canary" in recorded


def test_platform_backend_mapping_approves_only_native_backend_families(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Changing a platform mapping could silently allow a non-native credential backend."""
    macos = type("MacKeychain", (), {})
    windows = type("WindowsCredentialLocker", (), {})
    secret_service = type("LinuxSecretService", (), {})
    kwallet = type("LinuxKWallet", (), {})
    modules = {
        "keyring.backends.macOS": SimpleNamespace(Keyring=macos),
        "keyring.backends.Windows": SimpleNamespace(WinVaultKeyring=windows),
        "keyring.backends.SecretService": SimpleNamespace(Keyring=secret_service),
        "keyring.backends.kwallet": SimpleNamespace(DBusKeyring=kwallet),
    }
    monkeypatch.setattr(keyring_store.importlib, "import_module", modules.__getitem__)

    monkeypatch.setattr(keyring_store.sys, "platform", "darwin")
    assert keyring_store._approved_backend_types() == (macos,)
    monkeypatch.setattr(keyring_store.sys, "platform", "win32")
    assert keyring_store._approved_backend_types() == (windows,)
    monkeypatch.setattr(keyring_store.sys, "platform", "linux")
    assert keyring_store._approved_backend_types() == (secret_service, kwallet)


def test_platform_backend_mapping_rejects_unknown_and_unavailable_backends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(keyring_store.sys, "platform", "unknown")
    assert keyring_store._approved_backend_types() == ()
    monkeypatch.setattr(keyring_store.sys, "platform", "darwin")
    monkeypatch.setattr(
        keyring_store.importlib,
        "import_module",
        lambda _name: (_ for _ in ()).throw(ImportError()),
    )
    assert keyring_store._approved_backend_types() == ()


def test_keyring_rejects_missing_methods_invalid_public_inputs_and_nontext_load() -> None:
    incomplete = IncompleteBackend()
    with pytest.raises(CredentialStoreError):
        KeyringCredentialStore(_backend_factory=_factory(incomplete, type(incomplete)))

    backend = SecureMacBackend()
    store = KeyringCredentialStore(_backend_factory=_factory(backend))
    for method in ("load", "delete"):
        with pytest.raises(CredentialStoreError):
            getattr(store, method)(object())
    with pytest.raises(CredentialStoreError):
        store.save(CredentialKey("spotify", "id"), "")
    backend = NonTextBackend()
    store = KeyringCredentialStore(_backend_factory=_factory(backend, NonTextBackend))
    with pytest.raises(CredentialStoreError):
        store.load(CredentialKey("spotify", "id"))
