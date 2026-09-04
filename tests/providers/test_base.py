"""Behavioral tests for the provider-neutral music source contract."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime
from typing import Sequence

import pytest

from music_friend.domain import Artist, CatalogItemBatch, Release, SourceReference
from music_friend.errors import (
    AdditionalScopeRequiredError,
    CapabilityUnsupportedError,
)
from music_friend.providers.base import (
    Capability,
    HealthStatus,
    MusicSource,
    Page,
    ProviderCapabilities,
    ProviderHealth,
    require_capability,
)


class SpySource:
    """A source whose read methods prove a rejection happens before access."""

    def __init__(self, capabilities: ProviderCapabilities) -> None:
        self._capabilities = capabilities
        self.capabilities_calls = 0
        self.operation_calls = 0
        self.transport_calls = 0

    def capabilities(self) -> ProviderCapabilities:
        self.capabilities_calls += 1
        return self._capabilities

    def health(self) -> ProviderHealth:
        return ProviderHealth(HealthStatus.HEALTHY, self._capabilities)

    def _transport(self) -> None:
        self.transport_calls += 1

    def search_artists(self, query: str, limit: int) -> Page[Artist]:
        self.operation_calls += 1
        self._transport()
        return Page((), None)

    def followed_artists(self, cursor: str | None = None) -> Page[Artist]:
        self.operation_calls += 1
        self._transport()
        return Page((), None)

    def saved_items(self, cursor: str | None = None) -> CatalogItemBatch:
        self.operation_calls += 1
        self._transport()
        return CatalogItemBatch((), (), None)

    def top_items(self, time_range: str, limit: int) -> CatalogItemBatch:
        self.operation_calls += 1
        self._transport()
        return CatalogItemBatch((), (), None)

    def top_artists(self, time_range: str, limit: int) -> Page[Artist]:
        self.operation_calls += 1
        self._transport()
        return Page((), None)

    def recent_releases(
        self, artist_refs: Sequence[SourceReference], since: datetime
    ) -> Page[Release]:
        self.operation_calls += 1
        self._transport()
        return Page((), None)


class TransportingCapabilitiesSource:
    """A malformed source that would fetch over transport to learn its capabilities."""

    def __init__(self) -> None:
        self.capabilities_calls = 0
        self.operation_calls = 0
        self.transport_calls = 0

    def capabilities(self) -> ProviderCapabilities:
        self.capabilities_calls += 1
        self.transport_calls += 1
        return ProviderCapabilities({Capability.SAVED_ITEMS}, {Capability.SAVED_ITEMS})


def test_capabilities_are_immutable_and_grants_must_be_supported() -> None:
    """Catches mutable capability state and scopes granted without implementation."""
    capabilities = ProviderCapabilities(
        supported={Capability.SAVED_ITEMS, Capability.TOP_ITEMS},
        granted={Capability.SAVED_ITEMS},
    )

    assert capabilities.supported == frozenset({Capability.SAVED_ITEMS, Capability.TOP_ITEMS})
    assert capabilities.granted == frozenset({Capability.SAVED_ITEMS})
    with pytest.raises(FrozenInstanceError):
        capabilities.granted = frozenset()  # type: ignore[misc]
    with pytest.raises(ValueError, match="granted capabilities must be supported"):
        ProviderCapabilities({Capability.SAVED_ITEMS}, {Capability.TOP_ITEMS})


def test_page_requires_a_bounded_tuple_and_opaque_cursor() -> None:
    """Catches unbounded pages or cursors and conversion of opaque cursor contents."""
    cursor = "opaque:\u2603"
    page = Page(("first", "second"), cursor)

    assert page.items == ("first", "second")
    assert page.next_cursor == cursor
    with pytest.raises(ValueError, match="items must contain at most 500 entries"):
        Page(tuple(range(501)), None)
    with pytest.raises(ValueError, match="next_cursor must be at most 2048 characters"):
        Page((), "x" * 2049)
    with pytest.raises(ValueError, match="items must be a tuple"):
        Page(["not", "immutable"], None)  # type: ignore[arg-type]


def test_health_exposes_closed_safe_status_and_effective_grants_only() -> None:
    """Catches leaked diagnostics or health that advertises an ungranted operation."""
    capabilities = ProviderCapabilities(
        {Capability.SEARCH_ARTISTS, Capability.SAVED_ITEMS}, {Capability.SAVED_ITEMS}
    )
    health = ProviderHealth(HealthStatus.DEGRADED, capabilities)

    assert health.status is HealthStatus.DEGRADED
    assert health.granted_capabilities == frozenset({Capability.SAVED_ITEMS})
    assert health.effective_capabilities == frozenset({Capability.SAVED_ITEMS})
    assert not hasattr(health, "diagnostic_body")
    with pytest.raises(ValueError, match="status must be a HealthStatus"):
        ProviderHealth("server said healthy", capabilities)  # type: ignore[arg-type]


def test_music_source_protocol_accepts_a_complete_read_source() -> None:
    """Catches a protocol that omits one of the core read operations."""
    source = SpySource(ProviderCapabilities(frozenset(Capability), frozenset(Capability)))

    assert isinstance(source, MusicSource)


def test_music_source_has_no_duplicate_capability_snapshot_api() -> None:
    """Catches a second capability source that could diverge from capabilities()."""
    assert not hasattr(MusicSource, "capability_snapshot")


def test_require_capability_rejects_unsupported_before_adapter_access() -> None:
    """Catches unsupported source calls that reach an adapter or transport first."""
    capabilities = ProviderCapabilities({Capability.SAVED_ITEMS}, {Capability.SAVED_ITEMS})
    source = SpySource(capabilities)

    with pytest.raises(CapabilityUnsupportedError):
        require_capability(capabilities, Capability.TOP_ITEMS)

    assert source.capabilities_calls == 0
    assert source.operation_calls == 0
    assert source.transport_calls == 0


def test_require_capability_requests_more_scope_before_adapter_access() -> None:
    """Catches missing grants that reach an adapter or transport first."""
    capabilities = ProviderCapabilities({Capability.TOP_ITEMS}, frozenset())
    source = SpySource(capabilities)

    with pytest.raises(AdditionalScopeRequiredError):
        require_capability(capabilities, Capability.TOP_ITEMS)

    assert source.capabilities_calls == 0
    assert source.operation_calls == 0
    assert source.transport_calls == 0


def test_require_capability_allows_a_granted_operation() -> None:
    """Catches a capability gate that rejects an enabled, authorized operation."""
    capabilities = ProviderCapabilities({Capability.TOP_ITEMS}, {Capability.TOP_ITEMS})
    source = SpySource(capabilities)

    require_capability(capabilities, Capability.TOP_ITEMS)
    source.top_items("medium_term", 10)

    assert source.capabilities_calls == 0
    assert source.operation_calls == 1
    assert source.transport_calls == 1


def test_require_capability_does_not_touch_a_source_that_would_transport_for_capabilities() -> None:
    """Catches a gate that looks up capabilities through a source before rejection."""
    source = TransportingCapabilitiesSource()
    capabilities = ProviderCapabilities({Capability.SAVED_ITEMS}, {Capability.SAVED_ITEMS})

    with pytest.raises(CapabilityUnsupportedError):
        require_capability(capabilities, Capability.TOP_ITEMS)

    assert source.capabilities_calls == 0
    assert source.operation_calls == 0
    assert source.transport_calls == 0


def test_require_capability_requires_a_concrete_capability_value() -> None:
    """Catches a gate that accepts a source object or descriptor instead of a snapshot."""
    with pytest.raises(ValueError, match="capabilities must be ProviderCapabilities"):
        require_capability(object(), Capability.TOP_ITEMS)  # type: ignore[arg-type]
