"""Provider-neutral read contracts for Music Friend sources."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Generic, Protocol, Sequence, TypeVar, runtime_checkable

from music_friend.domain import Artist, CatalogItemBatch, Release, SourceReference
from music_friend.errors import (
    AdditionalScopeRequiredError,
    CapabilityUnsupportedError,
)

T = TypeVar("T")


class Capability(str, Enum):
    """Read operations a music source may support and be authorized to perform."""

    HEALTH = "health"
    SEARCH_ARTISTS = "search_artists"
    FOLLOWED_ARTISTS = "followed_artists"
    SAVED_ITEMS = "saved_items"
    TOP_ITEMS = "top_items"
    TOP_ARTISTS = "top_artists"
    RECENT_RELEASES = "recent_releases"


class HealthStatus(str, Enum):
    """Safe, closed source-health states."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


def _capability_set(value: object, name: str) -> frozenset[Capability]:
    if not isinstance(value, (set, frozenset)):
        raise ValueError(f"{name} must be a set of capabilities")
    capabilities = frozenset(value)
    if not all(isinstance(capability, Capability) for capability in capabilities):
        raise ValueError(f"{name} must contain capabilities")
    return capabilities


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    """Immutable provider support and authorization state."""

    supported: frozenset[Capability]
    granted: frozenset[Capability]

    def __post_init__(self) -> None:
        supported = _capability_set(self.supported, "supported")
        granted = _capability_set(self.granted, "granted")
        if not granted.issubset(supported):
            raise ValueError("granted capabilities must be supported")
        object.__setattr__(self, "supported", supported)
        object.__setattr__(self, "granted", granted)

    @property
    def effective(self) -> frozenset[Capability]:
        """Capabilities that are both implemented and authorized."""
        return self.granted


@dataclass(frozen=True, slots=True)
class ProviderHealth:
    """Safe status and effective capability state without provider diagnostics."""

    status: HealthStatus
    capabilities: ProviderCapabilities

    def __post_init__(self) -> None:
        if not isinstance(self.status, HealthStatus):
            raise ValueError("status must be a HealthStatus")
        if not isinstance(self.capabilities, ProviderCapabilities):
            raise ValueError("capabilities must be ProviderCapabilities")

    @property
    def granted_capabilities(self) -> frozenset[Capability]:
        """Capabilities authorized by the source's current grant."""
        return self.capabilities.granted

    @property
    def effective_capabilities(self) -> frozenset[Capability]:
        """Capabilities available for current source calls."""
        return self.capabilities.effective


@dataclass(frozen=True, slots=True)
class Page(Generic[T]):
    """A bounded source page with an opaque continuation cursor."""

    items: tuple[T, ...]
    next_cursor: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.items, tuple):
            raise ValueError("items must be a tuple")
        if len(self.items) > 500:
            raise ValueError("items must contain at most 500 entries")
        if self.next_cursor is not None:
            if not isinstance(self.next_cursor, str):
                raise ValueError("next_cursor must be a string or None")
            if len(self.next_cursor) > 2048:
                raise ValueError("next_cursor must be at most 2048 characters")


@runtime_checkable
class MusicSource(Protocol):
    """A read-only, provider-neutral source of normalized music records."""

    def capabilities(self) -> ProviderCapabilities:
        """Return the precomputed local capability snapshot without performing I/O."""
        ...

    def health(self) -> ProviderHealth: ...

    def search_artists(self, query: str, limit: int) -> Page[Artist]: ...

    def followed_artists(self, cursor: str | None = None) -> Page[Artist]: ...

    def saved_items(self, cursor: str | None = None) -> CatalogItemBatch: ...

    def top_items(self, time_range: str, limit: int) -> CatalogItemBatch: ...

    def top_artists(self, time_range: str, limit: int) -> Page[Artist]: ...

    def recent_releases(
        self,
        artist_refs: Sequence[SourceReference],
        since: datetime,
        cursor: str | None = None,
    ) -> Page[Release]: ...


def require_capability(capabilities: ProviderCapabilities, capability: Capability) -> None:
    """Reject an unavailable operation using a resolved local capability value."""
    if type(capabilities) is not ProviderCapabilities:
        raise ValueError("capabilities must be ProviderCapabilities")
    if not isinstance(capability, Capability):
        raise ValueError("capability must be a Capability")
    if capability not in capabilities.supported:
        raise CapabilityUnsupportedError()
    if capability not in capabilities.granted:
        raise AdditionalScopeRequiredError()


__all__ = [
    "Capability",
    "HealthStatus",
    "MusicSource",
    "Page",
    "ProviderCapabilities",
    "ProviderHealth",
    "require_capability",
]
